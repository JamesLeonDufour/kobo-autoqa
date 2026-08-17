"""The `supplement` dialect: KoboToolbox's current advanced-features API.

Newer kpi builds dropped `advanced_submission_post` and the `advanced_features`
blob on the asset. In their place:

    GET   /api/v2/assets/<uid>/advanced-features/          what is configured
    POST  /api/v2/assets/<uid>/advanced-features/          configure an action
    GET   /api/v2/assets/<uid>/data/<root_uuid>/supplement/   current results
    PATCH /api/v2/assets/<uid>/data/<root_uuid>/supplement/   request work

Three differences from the older dialects matter to the pipeline:

  * the submission uuid moved from the request body into the URL;
  * there is no `status: "requested"` -- issuing the PATCH *is* the request,
    and `language` is the only required field;
  * language and locale are separate fields, so "fr-FR" has to be split.

Results come back as an append-only list of `_versions`, newest state last,
each wrapping a `_data` object carrying `status` and (when complete) `value`.
"""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)

VERSION = "20250820"

ACTION_TRANSCRIBE = "automatic_google_transcription"
ACTION_TRANSLATE = "automatic_google_translation"
ACTION_QUAL = "automatic_bedrock_qual"

STATUS_IN_PROGRESS = "in_progress"
STATUS_COMPLETE = "complete"
STATUS_FAILED = "failed"
STATUS_DELETED = "deleted"


def split_language(code: str) -> tuple[str, str]:
    """"fr-FR" -> ("fr", "FR"). A bare "fr" yields an empty locale."""
    if not code:
        return "", ""
    parts = code.replace("_", "-").split("-", 1)
    return parts[0], (parts[1] if len(parts) > 1 else "")


# ---------------------------------------------------------------------------
# what the server has configured for this asset
# ---------------------------------------------------------------------------
class AssetFeatures:
    """The asset's advanced-features rows, grouped for the pipeline's use.

    This is the source of truth for *what* to request. The server owns it, and
    the qual question uuids in particular have to match exactly, so the
    pipeline reads them rather than inventing its own.
    """

    def __init__(self, rows: list[dict] | None = None) -> None:
        self.transcribe: dict[str, str] = {}          # xpath -> language
        self.translate: dict[str, list[str]] = {}     # xpath -> [language]
        self.qual: dict[str, list[str]] = {}          # xpath -> [question uuid]
        for row in rows or []:
            xpath = row.get("question_xpath")
            action = row.get("action")
            params = row.get("params") or []
            if not xpath or not action:
                continue
            if action == ACTION_TRANSCRIBE:
                langs = [p.get("language") for p in params if p.get("language")]
                if langs:
                    self.transcribe[xpath] = langs[0]
            elif action == ACTION_TRANSLATE:
                self.translate.setdefault(xpath, []).extend(
                    p["language"] for p in params if p.get("language")
                )
            elif action == ACTION_QUAL:
                self.qual.setdefault(xpath, []).extend(
                    p["uuid"] for p in params if p.get("uuid")
                )

    @property
    def xpaths(self) -> list[str]:
        return sorted(set(self.transcribe) | set(self.translate) | set(self.qual))

    def __bool__(self) -> bool:
        return bool(self.transcribe or self.translate or self.qual)

    def describe(self) -> str:
        return (f"transcribe={self.transcribe} translate={self.translate} "
                f"qual={ {k: len(v) for k, v in self.qual.items()} }")


# ---------------------------------------------------------------------------
# reading state out of a supplement document
# ---------------------------------------------------------------------------
def _latest_version(node: dict | None) -> dict:
    """The most recent entry of an action's append-only version list."""
    versions = (node or {}).get("_versions") or []
    if not versions:
        return {}
    # Newest last is the documented order; sort defensively when dates exist.
    if all(v.get("_dateCreated") for v in versions):
        versions = sorted(versions, key=lambda v: v["_dateCreated"])
    return versions[-1]


def _latest(node: dict | None) -> dict:
    """The most recent `_data` payload from an action's version list."""
    return _latest_version(node).get("_data") or {}


def _accepted(node: dict | None) -> bool:
    """A finished result is only usable downstream once it is accepted.

    Translation refuses to run against an unaccepted transcript -- the server
    answers `400 {"detail": "No transcription found"}` -- so the pipeline has
    to accept each result before moving on.
    """
    return bool(_latest_version(node).get("_dateAccepted"))


def _question(supplement: dict, xpath: str) -> dict:
    return (supplement or {}).get(xpath) or {}


def transcript_state(supplement: dict, xpath: str) -> tuple[str | None, str | None]:
    """(status, text). status is None when nothing has been requested yet."""
    data = _latest(_question(supplement, xpath).get(ACTION_TRANSCRIBE))
    if not data:
        return None, None
    return data.get("status"), data.get("value")


def transcript_accepted(supplement: dict, xpath: str) -> bool:
    return _accepted(_question(supplement, xpath).get(ACTION_TRANSCRIBE))


def translation_state(supplement: dict, xpath: str, language: str) -> str | None:
    node = (_question(supplement, xpath).get(ACTION_TRANSLATE) or {}).get(language)
    data = _latest(node)
    return data.get("status") if data else None


def translation_accepted(supplement: dict, xpath: str, language: str) -> bool:
    return _accepted((_question(supplement, xpath).get(ACTION_TRANSLATE) or {}).get(language))


def qual_state(supplement: dict, xpath: str, question_uuid: str) -> str | None:
    node = (_question(supplement, xpath).get(ACTION_QUAL) or {}).get(question_uuid)
    data = _latest(node)
    return data.get("status") if data else None


def is_pending(status: str | None) -> bool:
    return status == STATUS_IN_PROGRESS


def is_done(status: str | None) -> bool:
    return status == STATUS_COMPLETE


def needs_request(status: str | None) -> bool:
    """Nothing requested yet, or the last attempt failed / was deleted."""
    return status in (None, STATUS_FAILED, STATUS_DELETED)


# ---------------------------------------------------------------------------
# building PATCH bodies
# ---------------------------------------------------------------------------
def _envelope(xpath: str, action: str, body: dict) -> dict:
    return {"_version": VERSION, xpath: {action: body}}


def transcribe_body(xpath: str, language_code: str) -> dict:
    language, locale = split_language(language_code)
    body: dict[str, Any] = {"language": language}
    if locale:
        body["locale"] = locale
    return _envelope(xpath, ACTION_TRANSCRIBE, body)


def translate_body(xpath: str, language_code: str) -> dict:
    language, locale = split_language(language_code)
    body: dict[str, Any] = {"language": language}
    if locale:
        body["locale"] = locale
    return _envelope(xpath, ACTION_TRANSLATE, body)


def qual_body(xpath: str, question_uuid: str) -> dict:
    """One PATCH per question -- the schema takes a single uuid, not a list."""
    return _envelope(xpath, ACTION_QUAL, {"uuid": question_uuid})


def accept_transcript_body(xpath: str, language_code: str) -> dict:
    language, locale = split_language(language_code)
    body: dict[str, Any] = {"language": language, "accepted": True}
    if locale:
        body["locale"] = locale
    return _envelope(xpath, ACTION_TRANSCRIBE, body)


def accept_translation_body(xpath: str, language_code: str) -> dict:
    language, locale = split_language(language_code)
    body: dict[str, Any] = {"language": language, "accepted": True}
    if locale:
        body["locale"] = locale
    return _envelope(xpath, ACTION_TRANSLATE, body)
