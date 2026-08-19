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
import time
import uuid as uuid_lib
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)

VERSION = "20250820"

ACTION_TRANSCRIBE = "automatic_google_transcription"
ACTION_TRANSLATE = "automatic_google_translation"
ACTION_QUAL = "automatic_bedrock_qual"
# Holds the analysis *question definitions*. automatic_bedrock_qual then lists
# which of those questions the server should answer with AI.
ACTION_QUAL_DEFS = "manual_qual"

CHOICE_TYPES = {"qualSelectOne", "qualSelectMultiple"}
QUAL_TYPES = CHOICE_TYPES | {
    "qualText", "qualInteger", "qualTags", "qualNote", "qualAutoKeywordCount",
}

# Types the model can actually answer. The configuration endpoint happily
# stores any of them under automatic_bedrock_qual, but the trigger then
# rejects the ones that are not answerable:
#
#   qualTags            -> 400 "Invalid qualitative analysis question uuid"
#   qualNote            -> a heading for human coders, nothing to answer
#   qualAutoKeywordCount-> computed by the server, not by the model
#
# Verified against a live server: text, select-one, select-multiple and
# integer are accepted; tags are refused.
AUTO_QUAL_TYPES = CHOICE_TYPES | {"qualText", "qualInteger"}

# How many times to re-request an action the server has already failed. Most
# failures here are permanent (empty source text, target language == source),
# so one retry to cover a transient blip is enough before giving up and saying
# what the server said.
MAX_ACTION_FAILURES = 2

# How long an attempt may sit "in_progress" before we assume nobody is working
# on it. A transcription normally finishes well inside this; one that does not
# is usually not queued anywhere at all -- the server's own bulk-actions view
# reports no pending work while the submission still claims to be running --
# and waiting on it politely means waiting for ever.
STALL_SECONDS = 20 * 60

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
def _labels(value: Any) -> dict:
    """Accept a plain string or an existing {"_default": ...} label object."""
    if isinstance(value, dict):
        return {k: v for k, v in value.items() if isinstance(v, str)} or {"_default": ""}
    return {"_default": str(value or "")}


def _hint_block(value: Any) -> dict | None:
    """Normalise a hint into {"labels": {...}}, accepting a bare string too."""
    text = value.get("labels") if isinstance(value, dict) and "labels" in value else value
    if not text:
        return None
    labels = _labels(text)
    return {"labels": labels} if labels.get("_default") else None


def qual_definition(question: dict) -> dict:
    """Normalise one analysis question into the shape the server stores.

    Only the keys the schema allows are emitted -- it rejects anything else
    (`additionalProperties: false`), so stray UI fields have to be dropped.
    """
    qtype = question.get("type")
    if qtype not in QUAL_TYPES:
        raise ValueError(f"unsupported analysis question type: {qtype!r}")

    out: dict[str, Any] = {
        "uuid": question["uuid"],
        "type": qtype,
        "labels": _labels(question.get("labels") or question.get("label")),
    }
    hint = _hint_block(question.get("hint"))
    if hint:
        out["hint"] = hint
    if qtype in CHOICE_TYPES:
        choices = []
        for c in question.get("choices") or []:
            if not c.get("uuid"):
                continue
            choice = {"uuid": c["uuid"], "labels": _labels(c.get("labels") or c.get("label"))}
            # Choices carry their own hint, same shape as the question's.
            choice_hint = _hint_block(c.get("hint"))
            if choice_hint:
                choice["hint"] = choice_hint
            choices.append(choice)
        out["choices"] = choices
    return out


def qual_definitions(questions: list[dict]) -> list[dict]:
    return [qual_definition(q) for q in questions if q.get("uuid")]


def _match_existing(question: dict, existing: list[dict]) -> dict | None:
    """Find the same question among a recording's current definitions.

    By uuid first -- that is the recording being edited. Falling back to
    type + label matters when the same set is applied to a *second* recording:
    without it, every apply would mint new uuids and pile up duplicates.
    """
    by_uuid = {e.get("uuid"): e for e in existing}
    if question.get("uuid") in by_uuid:
        return by_uuid[question["uuid"]]
    label = _labels(question.get("labels") or question.get("label")).get("_default")
    for e in existing:
        if e.get("type") == question.get("type") and \
                (e.get("labels") or {}).get("_default") == label:
            return e
    return None


def localise_questions(questions: list[dict], existing: list[dict]) -> list[dict]:
    """Resolve a question set against one recording's existing configuration.

    Analysis answers are stored per recording under the question's uuid, so two
    recordings must not share uuids even when they ask the same thing. Each
    recording therefore keeps its own, reused when the question is already
    there and minted when it is not.
    """
    out: list[dict] = []
    for question in questions:
        match = _match_existing(question, existing)
        resolved = dict(question)
        resolved["uuid"] = (match or {}).get("uuid") or str(uuid_lib.uuid4())

        if question.get("choices"):
            old_choices = (match or {}).get("choices") or []
            by_label = {(c.get("labels") or {}).get("_default"): c for c in old_choices}
            by_uuid = {c.get("uuid"): c for c in old_choices}
            choices = []
            for choice in question["choices"]:
                label = _labels(choice.get("labels") or choice.get("label")).get("_default")
                prior = by_uuid.get(choice.get("uuid")) or by_label.get(label)
                choices.append({**choice,
                                "uuid": (prior or {}).get("uuid") or str(uuid_lib.uuid4())})
            resolved["choices"] = choices
        out.append(resolved)
    return out


def asr_variants(language_doc: dict) -> list[str]:
    """Regional codes Google can transcribe, e.g. ["fr-CA", "fr-FR"]."""
    services = (language_doc or {}).get("transcription_services") or {}
    codes: set[str] = set()
    for offered in services.values():
        codes.update(offered or {})
    return sorted(codes)


def translatable(language_doc: dict) -> bool:
    services = (language_doc or {}).get("translation_services") or {}
    return any(offered for offered in services.values())


def undeletable(existing: list[dict], submitted: list[dict]) -> list[str]:
    """Labels of questions on the form that an edit tried to drop.

    Removing a question in the editor cannot remove it from KoboToolbox: the
    configuration row accepts additions and nothing else. Naming them lets the
    UI say so, instead of appearing to delete something and leaving it there
    being answered on every submission.
    """
    kept = {q.get("uuid") for q in submitted}
    labels = []
    for q in existing:
        if q.get("uuid") not in kept:
            labels.append((q.get("labels") or {}).get("_default") or q.get("uuid", "")[:8])
    return labels


def usable_targets(source: str, targets: list[str] | None) -> tuple[list[str], list[str]]:
    """Split translation targets into those worth requesting and those not.

    Translating a language into itself is rejected by Google with
    `400 Target language can't be equal to source language`, and the rejection
    repeats for every submission. Only the base code matters: fr-FR audio
    cannot be translated to fr, however either side is written.
    """
    base = split_language(source)[0].lower()
    keep: list[str] = []
    same: list[str] = []
    for target in targets or []:
        if base and split_language(target)[0].lower() == base:
            same.append(target)
        else:
            keep.append(target)
    return keep, same


def auto_qual_params(questions: list[dict]) -> list[dict]:
    """Which questions the AI should answer -- see AUTO_QUAL_TYPES.

    Listing an unanswerable type here is accepted by the configuration
    endpoint and only fails later, once per submission, when the pipeline
    tries to trigger it. So it has to be filtered at write time.
    """
    return [{"uuid": q["uuid"]} for q in questions
            if q.get("uuid") and q.get("type") in AUTO_QUAL_TYPES]


class AssetFeatures:
    """The asset's advanced-features rows, grouped for the pipeline's use.

    This is the source of truth for *what* to request. The server owns it, and
    the qual question uuids in particular have to match exactly, so the
    pipeline reads them rather than inventing its own.
    """

    def __init__(self, rows: list[dict] | None = None) -> None:
        self.rows: list[dict] = list(rows or [])
        self.transcribe: dict[str, str] = {}          # xpath -> language
        self.translate: dict[str, list[str]] = {}     # xpath -> [language]
        self.qual: dict[str, list[str]] = {}          # xpath -> [question uuid]
        self.definitions: dict[str, list[dict]] = {}  # xpath -> [question def]
        self.superseded: dict[str, list[str]] = {}    # xpath -> older audio languages
        for row in rows or []:
            xpath = row.get("question_xpath")
            action = row.get("action")
            params = row.get("params") or []
            if not xpath or not action:
                continue
            if action == ACTION_TRANSCRIBE:
                langs = [p.get("language") for p in params if p.get("language")]
                if langs:
                    # Params are append-only: changing the audio language adds
                    # to this row instead of replacing it, so a form edited
                    # from fr to en holds ["fr", "en"]. The newest is the one
                    # the user last asked for, and only one can apply.
                    self.transcribe[xpath] = langs[-1]
                    if len(langs) > 1:
                        self.superseded[xpath] = langs[:-1]
                        log.info("%s: audio language %s, superseding %s "
                                 "(KoboToolbox cannot delete the earlier ones)",
                                 xpath, langs[-1], ", ".join(langs[:-1]))
            elif action == ACTION_TRANSLATE:
                self.translate.setdefault(xpath, []).extend(
                    p["language"] for p in params if p.get("language")
                )
            elif action == ACTION_QUAL:
                self.qual.setdefault(xpath, []).extend(
                    p["uuid"] for p in params if p.get("uuid")
                )
            elif action == ACTION_QUAL_DEFS:
                self.definitions.setdefault(xpath, []).extend(
                    p for p in params if p.get("uuid")
                )

    @property
    def xpaths(self) -> list[str]:
        return sorted(set(self.transcribe) | set(self.translate) | set(self.qual))

    def question_type(self, xpath: str, question_uuid: str) -> str | None:
        for q in self.definitions.get(xpath, []):
            if q.get("uuid") == question_uuid:
                return q.get("type")
        return None

    def answerable_qual(self, xpath: str) -> list[str]:
        """Auto-answer uuids the model can actually handle.

        Two ways a uuid gets here that the server will then refuse, both of
        them permanent because advanced-features rows are append-only:

          * a type the model cannot answer, such as qualTags, which a hand-made
            configuration can list quite happily;
          * a uuid with no definition at all -- the question was removed from
            manual_qual, or never existed there, and the auto-answer entry
            could not be removed alongside it.

        Both earn `400 Invalid qualitative analysis question uuid` on every
        submission, forever. Skipping them locally is the only remedy, since
        the configuration itself cannot be corrected.
        """
        out = []
        for question_uuid in self.qual.get(xpath, []):
            qtype = self.question_type(xpath, question_uuid)
            if qtype is None:
                log.warning("Skipping %s on %s: nothing defines it any more "
                            "(the auto-answer entry cannot be deleted server-side)",
                            question_uuid[:8], xpath)
                continue
            if qtype in AUTO_QUAL_TYPES:
                out.append(question_uuid)
            else:
                log.warning("Skipping %s question %s on %s: not auto-answerable",
                            qtype, question_uuid[:8], xpath)
        return out

    def row_uid(self, xpath: str, action: str) -> str | None:
        for row in self.rows:
            if row.get("question_xpath") == xpath and row.get("action") == action:
                return row.get("uid")
        return None

    def __bool__(self) -> bool:
        return bool(self.transcribe or self.translate or self.qual)

    def describe(self) -> str:
        return (f"transcribe={self.transcribe} translate={self.translate} "
                f"qual={ {k: len(v) for k, v in self.qual.items()} }")


# ---------------------------------------------------------------------------
# reading state out of a supplement document
# ---------------------------------------------------------------------------
def _ordered_versions(node: dict | None) -> list[dict]:
    """An action's version list, oldest first.

    The order is not something to rely on: a live server returns newest first,
    while the documentation implies the opposite. Every real version carries
    `_dateCreated`, so sort on it and only fall back to list order when it is
    missing.
    """
    versions = list((node or {}).get("_versions") or [])
    if versions and all(v.get("_dateCreated") for v in versions):
        versions.sort(key=lambda v: v["_dateCreated"])
    return versions


def _latest_version(node: dict | None) -> dict:
    """The most recent entry of an action's append-only version list."""
    versions = _ordered_versions(node)
    return versions[-1] if versions else {}


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


def transcript_node(supplement: dict, xpath: str) -> dict:
    return _question(supplement, xpath).get(ACTION_TRANSCRIBE) or {}


def translation_node(supplement: dict, xpath: str, language: str) -> dict:
    return (_question(supplement, xpath).get(ACTION_TRANSLATE) or {}).get(language) or {}


def qual_node(supplement: dict, xpath: str, question_uuid: str) -> dict:
    return (_question(supplement, xpath).get(ACTION_QUAL) or {}).get(question_uuid) or {}


def failure_streak(node: dict | None) -> int:
    """How many times in a row the most recent attempts have failed.

    Every request appends a version, so the document itself records how often
    we have already tried. That is the whole retry state -- no bookkeeping of
    our own, and it survives a restart.
    """
    streak = 0
    for version in reversed(_ordered_versions(node)):
        if (version.get("_data") or {}).get("status") == STATUS_FAILED:
            streak += 1
        else:
            break
    return streak


def error_text(node: dict | None) -> str:
    """The server's own explanation for the latest attempt, if it failed."""
    return str((_latest(node) or {}).get("error") or "").strip()


def transcript_text(supplement: dict, xpath: str) -> str:
    return str(_latest(transcript_node(supplement, xpath)).get("value") or "")


def transcript_is_empty(supplement: dict, xpath: str) -> bool:
    """A transcript that finished with no words in it.

    Google reports this as a *successful* transcription with an empty value.
    Accepting it would send an empty string to translation, which answers
    `400 Empty request` for as long as anything keeps asking.
    """
    status, _ = transcript_state(supplement, xpath)
    return is_done(status) and not transcript_text(supplement, xpath).strip()


def _parsed_date(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(
            str(value).replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()
    except ValueError:
        return None


def stalled_for(node: dict | None) -> float:
    """Seconds the current in-progress attempt has been running, else 0."""
    version = _latest_version(node)
    if (version.get("_data") or {}).get("status") != STATUS_IN_PROGRESS:
        return 0.0
    started = _parsed_date(version.get("_dateCreated"))
    return max(0.0, time.time() - started) if started else 0.0


def is_stalled(node: dict | None, seconds: float = STALL_SECONDS) -> bool:
    """An attempt that has been running long enough to count as abandoned."""
    return stalled_for(node) > seconds


def is_failed(status: str | None) -> bool:
    return status == STATUS_FAILED


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
    """Request transcription in one language.

    The regional code goes in `language` whole, and there is no `locale` field
    in the request at all -- `{"language": "en-GB"}`. The *stored* result comes
    back split as `{"language": "en", "locale": "en-GB"}`, which is not the
    shape to send: sending it earns `400 Invalid payload`, as does sending the
    bare region subtag.
    """
    return _envelope(xpath, ACTION_TRANSCRIBE, {"language": language_code})


def translate_body(xpath: str, language_code: str) -> dict:
    # Translation targets are bare: /api/v2/languages/fr/ lists exactly one
    # translation service code, "fr", with no regional variants at all.
    language, _ = split_language(language_code)
    return _envelope(xpath, ACTION_TRANSLATE, {"language": language})


def qual_body(xpath: str, question_uuid: str) -> dict:
    """One PATCH per question -- the schema takes a single uuid, not a list."""
    return _envelope(xpath, ACTION_QUAL, {"uuid": question_uuid})


# ---------------------------------------------------------------------------
# writing configuration
# ---------------------------------------------------------------------------
def put_row(client, asset_uid: str, features: "AssetFeatures",
            xpath: str, action: str, params: list[dict]) -> None:
    """Create or replace one advanced-features row.

    Rows are identified by (question_xpath, action); `params` replaces what
    was there, so callers pass the complete desired list.
    """
    uid = features.row_uid(xpath, action)
    if uid:
        client.update_advanced_feature(asset_uid, uid, params)
    else:
        client.create_advanced_feature(
            asset_uid, question_xpath=xpath, action=action, params=params)


def sync_question(client, asset_uid: str, features: "AssetFeatures", xpath: str, *,
                  transcribe_language: str = "",
                  translate_languages: list[str] | None = None,
                  questions: list[dict] | None = None,
                  enable_qual: bool = True, merge: bool = False) -> list[dict]:
    """Apply a full configuration to one audio question.

    Everything here is scoped to a single `question_xpath` because that is how
    the server models it: analysis questions belong to one audio question, not
    to the form. Returns the questions as stored for this recording, whose
    uuids are its own.

    This only ever *adds*. Advanced-features params are append-only, so a
    question left out of `questions` stays on the form and keeps being
    answered; see `undeletable()` for reporting that back to whoever is
    editing.
    """
    if transcribe_language:
        # Store the regional code as given. Stripping it to the base language
        # discarded the user's choice of recognition model, and the row is
        # append-only so the mistake could not be undone afterwards.
        put_row(client, asset_uid, features, xpath, ACTION_TRANSCRIBE,
                [{"language": transcribe_language}])

    if translate_languages is not None:
        put_row(client, asset_uid, features, xpath, ACTION_TRANSLATE,
                [{"language": split_language(l)[0]} for l in translate_languages])

    if questions is not None:
        existing = features.definitions.get(xpath, [])
        # Resolve uuids against this recording so the same set can be applied
        # to several without them sharing uuids or duplicating on re-apply.
        local = localise_questions(questions, existing)
        if merge:
            # Kept for the payload's sake, though the distinction is weaker
            # than it looks: the server merges params rather than replacing
            # them, so *nothing* sent here can remove a question from any
            # recording. `merge=False` does not strip the edited one either.
            incoming = {q["uuid"] for q in local}
            local = local + [q for q in existing if q.get("uuid") not in incoming]
        put_row(client, asset_uid, features, xpath, ACTION_QUAL_DEFS,
                qual_definitions(local))
        put_row(client, asset_uid, features, xpath, ACTION_QUAL,
                auto_qual_params(local) if enable_qual else [])
        return local
    return list(questions or [])


def accept_transcript_body(xpath: str, language_code: str) -> dict:
    return _envelope(xpath, ACTION_TRANSCRIBE,
                     {"language": language_code, "accepted": True})


def accept_translation_body(xpath: str, language_code: str) -> dict:
    language, _ = split_language(language_code)
    return _envelope(xpath, ACTION_TRANSLATE, {"language": language, "accepted": True})
