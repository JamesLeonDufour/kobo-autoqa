"""Payload builders / readers for the advanced_submission_post endpoint.

The subsequences API has two payload dialects in the wild:

  legacy    kpi 2.024.x - 2.026.x. Async jobs are requested by posting a
            `googlets` (speech-to-text) or `googletx` (translate) object with
            status="requested" under the question's xpath.

  20250820  the newer subsequences schema (SCHEMA_VERSIONS in
            kobo/apps/subsequences/constants.py). Same envelope, but actions
            are addressed by name.

`detect_dialect()` picks one from the live JSON schema returned by
/advanced_submission_schema/, so you do not have to hard-code a guess. Run
`python -m app.cli introspect <asset_uid>` to print what your server accepts.
"""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)

LEGACY = "legacy"
V20250820 = "20250820"
# Current kpi: advanced-features + data/<uuid>/supplement/. See app/supplement.py.
SUPPLEMENT = "supplement"

STATUS_REQUESTED = "requested"
STATUS_IN_PROGRESS = "in_progress"
STATUS_COMPLETE = "complete"
STATUS_ERROR = "error"
TERMINAL_OK = {STATUS_COMPLETE}
TERMINAL_BAD = {STATUS_ERROR}

MEDIA_TYPES = {"audio", "video", "background-audio"}


# --------------------------------------------------------------------------
# dialect detection
# --------------------------------------------------------------------------
def detect_dialect(schema: dict | None) -> str:
    """Inspect the live advanced_submission_schema and choose a dialect."""
    blob = repr(schema or "")
    if "googlets" in blob or "googletx" in blob:
        return LEGACY
    if "20250820" in blob or "automatic_google_transcription" in blob:
        return V20250820
    log.warning("Could not detect payload dialect from schema; defaulting to %s", LEGACY)
    return LEGACY


# --------------------------------------------------------------------------
# question discovery
# --------------------------------------------------------------------------
def media_question_xpaths(asset: dict) -> list[str]:
    """Return xpaths of transcribable questions in the form definition."""
    survey = ((asset or {}).get("content") or {}).get("survey") or []
    groups: list[str] = []
    out: list[str] = []
    for row in survey:
        rtype = row.get("type")
        name = row.get("$autoname") or row.get("name")
        if rtype in ("begin_group", "begin_repeat"):
            if name:
                groups.append(name)
            continue
        if rtype in ("end_group", "end_repeat"):
            if groups:
                groups.pop()
            continue
        if rtype in MEDIA_TYPES and name:
            out.append("/".join(groups + [name]))
    return out


def configured_xpaths(advanced_features: dict, fallback: list[str]) -> list[str]:
    """Questions the asset already has advanced features enabled on."""
    xpaths: set[str] = set()
    for key in ("transcript", "translation", "qual"):
        node = (advanced_features or {}).get(key) or {}
        for x in node.get("values") or []:
            if isinstance(x, str):
                xpaths.add(x)
    return sorted(xpaths) or fallback


def qual_survey(advanced_features: dict) -> list[dict]:
    return ((advanced_features or {}).get("qual") or {}).get("qual_survey") or []


# --------------------------------------------------------------------------
# state readers
# --------------------------------------------------------------------------
def _q(supplement: dict, xpath: str) -> dict:
    return (supplement or {}).get(xpath) or {}


def transcript_status(supplement: dict, xpath: str) -> tuple[str | None, str | None]:
    """Return (status, text). status is None when nothing was ever requested."""
    node = _q(supplement, xpath)
    transcript = node.get("transcript") or {}
    if transcript.get("value"):
        return STATUS_COMPLETE, transcript["value"]
    auto = node.get("googlets") or node.get("automatic_google_transcription") or {}
    status = auto.get("status")
    return (status, auto.get("value")) if status else (None, None)


def translation_status(supplement: dict, xpath: str, lang: str) -> str | None:
    node = _q(supplement, xpath)
    translations = node.get("translation") or {}
    entry = translations.get(lang) or {}
    if entry.get("value"):
        return STATUS_COMPLETE
    auto = node.get("googletx") or node.get("automatic_google_translation") or {}
    if auto.get("languageCode") == lang:
        return auto.get("status")
    return None


def qual_answers(supplement: dict, xpath: str) -> list[dict]:
    node = _q(supplement, xpath)
    quals = node.get("qual")
    if isinstance(quals, list):
        return quals
    if isinstance(quals, dict):
        return list(quals.values())
    return []


def qual_complete(supplement: dict, xpath: str, expected: list[dict]) -> bool:
    """True when every non-note preset question has a non-empty answer."""
    wanted = {
        q.get("uuid") for q in expected
        if q.get("uuid") and q.get("type") != "qualNote"
    }
    if not wanted:
        return True
    answered = {
        a.get("uuid") for a in qual_answers(supplement, xpath)
        if a.get("uuid") and a.get("val") not in (None, "", [], {})
    }
    return wanted.issubset(answered)


# --------------------------------------------------------------------------
# payload builders
# --------------------------------------------------------------------------
def transcribe_payload(dialect: str, submission_uuid: str, xpath: str, language: str) -> dict:
    if dialect == V20250820:
        action: dict[str, Any] = {
            "automatic_google_transcription": {
                "status": STATUS_REQUESTED,
                "languageCode": language,
            }
        }
    else:
        action = {"googlets": {"status": STATUS_REQUESTED, "languageCode": language}}
    return {"submission": submission_uuid, xpath: action}


def translate_payload(dialect: str, submission_uuid: str, xpath: str, language: str) -> dict:
    if dialect == V20250820:
        action: dict[str, Any] = {
            "automatic_google_translation": {
                "status": STATUS_REQUESTED,
                "languageCode": language,
            }
        }
    else:
        action = {"googletx": {"status": STATUS_REQUESTED, "languageCode": language}}
    return {"submission": submission_uuid, xpath: action}


def qual_payload(
    dialect: str,
    submission_uuid: str,
    xpath: str,
    survey: list[dict],
    *,
    trigger_key: str = "qual",
    source_language: str = "",
) -> dict:
    """Request automatic (Bedrock) answers for the preset qual questions.

    Every preset question is submitted with an empty value and
    `source: "generated with AI"`, which is what tells kpi to fill it in via
    Bedrock rather than treat it as a manual answer.
    """
    items = []
    for q in survey:
        if q.get("type") == "qualNote" or not q.get("uuid"):
            continue
        item: dict[str, Any] = {
            "uuid": q["uuid"],
            "type": q["type"],
            "val": None,
            "source": "generated with AI",
        }
        if source_language:
            item["languageCode"] = source_language
        items.append(item)

    if dialect == V20250820:
        node: dict[str, Any] = {
            "automatic_bedrock_qual": {"status": STATUS_REQUESTED, "qual": items}
        }
        if source_language:
            node["automatic_bedrock_qual"]["languageCode"] = source_language
    else:
        node = {trigger_key: items}
    return {"submission": submission_uuid, xpath: node}
