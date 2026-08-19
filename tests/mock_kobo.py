"""Minimal in-process fake KoboToolbox server for local testing.

    python tests/mock_kobo.py          # serves on http://127.0.0.1:8899

Then point the pipeline at it:

    KOBO_URL=http://127.0.0.1:8899 KOBO_TOKEN=x ADMIN_PASSWORD=test \
    ADMIN_COOKIE_SECURE=false DB_PATH=/tmp/mock.db \
    uvicorn app.webhook:app --port 8000
"""
from __future__ import annotations

import copy
import uuid

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import HTMLResponse

app = FastAPI()

ASSET_UID = "aMOCK1234567890abcdef"

STATE = {
    "advanced_features": {},
    "hooks": [],
    "supplements": {},
    # "legacy"     -> advanced_submission_post, like kpi 2.024-2.026
    # "supplement" -> advanced-features + data/<uuid>/supplement/, like current
    #                 kpi, where the older endpoints are gone entirely
    "api_mode": "legacy",
    "features": [],       # advanced-features rows
    "supplements_v2": {},  # root_uuid -> supplement document
}

QUAL_UUIDS = ["11111111-1111-4111-8111-111111111111",
              "22222222-2222-4222-8222-222222222222"]

SURVEY = [
    {"type": "start", "name": "start"},
    {"type": "begin_group", "name": "section_a", "$autoname": "section_a"},
    {"type": "audio", "name": "Recording_001", "$autoname": "Recording_001",
     "label": ["Tell us about your situation"]},
    {"type": "end_group"},
    {"type": "background-audio", "name": "ambient", "$autoname": "ambient"},
    {"type": "text", "name": "notes", "$autoname": "notes"},
]

SCHEMA = {
    "$description": "mock advanced_submission_schema",
    "type": "object",
    "properties": {
        "submission": {"type": "string"},
        "section_a/Recording_001": {
            "type": "object",
            "properties": {
                "transcript": {"type": "object"},
                "googlets": {"type": "object", "properties": {"status": {}, "languageCode": {}}},
                "googletx": {"type": "object", "properties": {"status": {}, "languageCode": {}}},
                "qual": {"type": "array"},
            },
        },
    },
}


@app.post("/__reset")
def reset():
    """Test-only: clear all mock state so a suite starts from a clean server."""
    STATE["advanced_features"] = {}
    STATE["hooks"] = []
    STATE["supplements"] = {}
    STATE["api_mode"] = "legacy"
    STATE["features"] = []
    STATE["supplements_v2"] = {}
    return {"ok": True}


@app.post("/__mode/{mode}")
def set_mode(mode: str, preconfigure: bool = True):
    """Test-only: switch which generation of the NLP API this server pretends
    to be. `supplement` also seeds a configuration, the way a real server does
    when someone has already set the form up in Kobo's own UI."""
    STATE["api_mode"] = mode
    STATE["features"] = []
    STATE["supplements_v2"] = {}
    if mode == "supplement" and preconfigure:
        STATE["features"] = [
            {"uid": "qaf1", "question_xpath": "section_a/Recording_001",
             "action": "automatic_google_transcription", "params": [{"language": "fr"}]},
            {"uid": "qaf2", "question_xpath": "section_a/Recording_001",
             "action": "automatic_google_translation",
             "params": [{"language": "en"}, {"language": "es"}]},
            {"uid": "qaf3", "question_xpath": "section_a/Recording_001",
             "action": "automatic_bedrock_qual",
             "params": [{"uuid": u} for u in QUAL_UUIDS]},
            # A real form always defines the questions it asks the model to
            # answer. Seeding auto-answer uuids without definitions described a
            # server that cannot exist, and hid the fact that an *orphaned*
            # uuid is refused on every submission.
            {"uid": "qaf4", "question_xpath": "section_a/Recording_001",
             "action": "manual_qual",
             "params": [{"uuid": u, "type": "qualText",
                         "labels": {"_default": f"Seeded question {i + 1}"}}
                        for i, u in enumerate(QUAL_UUIDS)]},
        ]
    return {"ok": True, "mode": mode, "features": len(STATE["features"])}


@app.get("/me/")
def me():
    return {"username": "mockuser", "email": "mock@example.org"}


@app.get("/api/v2/assets/")
def assets():
    return {"count": 1, "results": [{
        "uid": ASSET_UID, "name": "Mock qualitative survey", "asset_type": "survey",
        "has_deployment": True, "deployment__submission_count": 42,
        "advanced_features": STATE["advanced_features"],
    }]}


@app.get("/api/v2/assets/{uid}/")
def asset(uid: str):
    return {
        "uid": uid, "name": "Mock qualitative survey",
        "has_deployment": True, "deployment__submission_count": 42,
        "content": {"survey": SURVEY},
        "advanced_features": STATE["advanced_features"],
    }


@app.patch("/api/v2/assets/{uid}/")
def patch_asset(uid: str, body: dict = Body(...)):
    if "advanced_features" in body:
        STATE["advanced_features"] = body["advanced_features"]
    return asset(uid)


@app.get("/api/v2/assets/{uid}/advanced_submission_schema/")
def schema(uid: str):
    if STATE["api_mode"] == "supplement":
        # Current kpi does not route this at all; it falls through to the SPA.
        return HTMLResponse("<!doctype html><html>Not found</html>", status_code=404)
    return SCHEMA


# ---------------------------------------------------------------------------
# current kpi API: advanced-features + per-submission supplement
# ---------------------------------------------------------------------------
def _require_new_api():
    if STATE["api_mode"] != "supplement":
        raise HTTPException(status_code=404, detail="Resource not found (404)")


def _require_legacy_api():
    """A current server has removed these endpoints entirely."""
    if STATE["api_mode"] == "supplement":
        raise HTTPException(status_code=404, detail="Resource not found (404)")


@app.get("/api/v2/assets/{uid}/advanced-features/")
def list_features(uid: str):
    _require_new_api()
    return STATE["features"]


@app.post("/api/v2/assets/{uid}/advanced-features/")
def create_feature(uid: str, body: dict = Body(...)):
    _require_new_api()
    if not body.get("action") or not body.get("question_xpath"):
        raise HTTPException(status_code=400, detail="action and question_xpath required")
    _validate_params(body["action"], body.get("params") or [])
    row = {**body, "uid": "qaf" + uuid.uuid4().hex[:16]}
    STATE["features"].append(row)
    return row


def _identity(param: dict) -> str:
    """What the server treats as "the same parameter" when merging.

    Language rows are keyed on the language; qual rows on the question uuid.
    """
    return str(param.get("language") or param.get("uuid") or id(param))


def _merge_params(existing: list, incoming: list) -> list:
    """Params as a real server updates them: additive, never subtractive.

    This is the behaviour the mock used to get wrong, and getting it wrong hid
    three separate bugs that reached production -- an audio language that
    would not change, a translation target that could not be removed, and the
    empty transcript that followed from the first. Sending a shorter list does
    not delete anything; it is a no-op for whatever is missing from it.
    """
    merged = list(existing)
    by_identity = {_identity(p): i for i, p in enumerate(merged)}
    for param in incoming:
        key = _identity(param)
        if key in by_identity:
            merged[by_identity[key]] = param
        else:
            merged.append(param)
    return merged


def _update_feature(feature_uid: str, body: dict) -> dict:
    for row in STATE["features"]:
        if row["uid"] == feature_uid:
            if "params" in body:
                _validate_params(row["action"], body["params"])
                row["params"] = _merge_params(row["params"], body["params"])
            return row
    raise HTTPException(status_code=404, detail="Resource not found (404)")


@app.patch("/api/v2/assets/{uid}/advanced-features/{feature_uid}/")
def patch_feature(uid: str, feature_uid: str, body: dict = Body(...)):
    _require_new_api()
    return _update_feature(feature_uid, body)


@app.put("/api/v2/assets/{uid}/advanced-features/{feature_uid}/")
def put_feature(uid: str, feature_uid: str, body: dict = Body(...)):
    """PUT merges exactly as PATCH does -- a full body does not replace."""
    _require_new_api()
    return _update_feature(feature_uid, body)


@app.delete("/api/v2/assets/{uid}/advanced-features/{feature_uid}/")
def delete_feature(uid: str, feature_uid: str):
    """The real server does not offer this: configuration cannot be removed."""
    _require_new_api()
    raise HTTPException(status_code=405, detail='Method "DELETE" not allowed.')


CHOICE_TYPES = {"qualSelectOne", "qualSelectMultiple"}
QUAL_TYPES = CHOICE_TYPES | {"qualText", "qualInteger", "qualTags", "qualNote",
                             "qualAutoKeywordCount"}
ALLOWED = {"uuid", "type", "labels", "hint", "choices"}


def _validate_transcribe_request(xpath: str, body: dict) -> None:
    """`language` must be a base code the advanced-features row lists.

    A regional code in `language` is refused, and so is a base language the
    row does not carry -- the row is the authority on what may be requested.
    """
    lang = str(body.get("language") or "")
    if "-" in lang:
        raise HTTPException(status_code=400, detail="Invalid payload")
    configured = {p.get("language") for row in STATE["features"]
                  if row.get("question_xpath") == xpath
                  and row.get("action") == "automatic_google_transcription"
                  for p in (row.get("params") or [])}
    if configured and lang not in configured:
        raise HTTPException(status_code=400, detail="Invalid payload")


def _validate_params(action: str, params: list):
    """Mimic the server's strict schema: unknown keys are rejected outright."""
    for p in params:
        if action == "manual_qual":
            extra = set(p) - ALLOWED
            if extra:
                raise HTTPException(status_code=400,
                                    detail=f"unexpected keys {sorted(extra)}")
            if p.get("type") not in QUAL_TYPES:
                raise HTTPException(status_code=400, detail=f"bad type {p.get('type')}")
            if not (p.get("labels") or {}).get("_default"):
                raise HTTPException(status_code=400, detail="labels._default required")
            if p["type"] in CHOICE_TYPES and not p.get("choices"):
                raise HTTPException(status_code=400, detail="choices required")
            for ch in p.get("choices") or []:
                # The real schema is additionalProperties:false here too.
                extra = set(ch) - {"uuid", "labels", "hint"}
                if extra:
                    raise HTTPException(status_code=400,
                                        detail=f"unexpected choice keys {sorted(extra)}")
                if not (ch.get("labels") or {}).get("_default"):
                    raise HTTPException(status_code=400,
                                        detail="choice labels._default required")
        elif action in ("automatic_bedrock_qual",):
            if set(p) != {"uuid"}:
                raise HTTPException(status_code=400, detail="only uuid allowed")
        elif action in ("automatic_google_transcription", "automatic_google_translation"):
            if set(p) - {"language"}:
                raise HTTPException(status_code=400, detail="only language allowed")


def _version(data: dict) -> dict:
    # A fresh version is unaccepted: real servers omit _dateAccepted entirely.
    return {"_dateCreated": "2026-08-17T12:00:00Z",
            "_uuid": str(uuid.uuid4()), "_data": data}


def _is_accepted(slot: dict | None) -> bool:
    versions = (slot or {}).get("_versions") or []
    return bool(versions and versions[-1].get("_dateAccepted"))


def _accept(slot: dict) -> None:
    versions = slot.get("_versions") or []
    if not versions:
        raise HTTPException(status_code=400, detail="nothing to accept")
    if (versions[-1].get("_data") or {}).get("status") != "complete":
        raise HTTPException(status_code=400, detail="cannot accept an unfinished job")
    versions[-1]["_dateAccepted"] = "2026-08-17T12:00:05Z"


def _advance(node: dict, done: dict) -> None:
    """Move an in_progress action to complete, mimicking Kobo finishing a job."""
    last = (node.get("_versions") or [{}])[-1].get("_data") or {}
    if last.get("status") == "in_progress":
        node["_versions"].append(_version({**last, **done, "status": "complete"}))


@app.get("/api/v2/assets/{uid}/data/{root_uuid}/supplement/")
def get_supplement_v2(uid: str, root_uuid: str):
    _require_new_api()
    doc = STATE["supplements_v2"].get(root_uuid)
    if doc is None:
        return {}
    # Anything requested since the last poll is now finished.
    for xpath, actions in doc.items():
        if xpath == "_version":
            continue
        if "automatic_google_transcription" in actions:
            _advance(actions["automatic_google_transcription"],
                     {"value": "Bonjour, la situation est difficile."})
        for lang, node in (actions.get("automatic_google_translation") or {}).items():
            _advance(node, {"value": f"[{lang}] Hello, the situation is difficult."})
        for quid, node in (actions.get("automatic_bedrock_qual") or {}).items():
            _advance(node, {"uuid": quid, "value": "AI answer"})
    return doc


@app.patch("/api/v2/assets/{uid}/data/{root_uuid}/supplement/")
def patch_supplement_v2(uid: str, root_uuid: str, body: dict = Body(...)):
    _require_new_api()
    if body.get("_version") != "20250820":
        raise HTTPException(status_code=400, detail="_version is required")
    doc = STATE["supplements_v2"].setdefault(root_uuid, {"_version": "20250820"})
    for xpath, actions in body.items():
        if xpath == "_version":
            continue
        node = doc.setdefault(xpath, {})
        for action, payload in actions.items():
            if action == "automatic_google_transcription":
                slot = node.setdefault(action, {"_versions": []})
                if payload.get("accepted"):
                    _accept(slot)
                    continue
                _validate_transcribe_request(xpath, payload)
                slot["_versions"].append(_version({**payload, "status": "in_progress"}))
            elif action == "automatic_google_translation":
                lang = payload.get("language")
                if not lang:
                    raise HTTPException(status_code=400, detail="language is required")
                slot = node.setdefault(action, {}).setdefault(lang, {"_versions": []})
                if payload.get("accepted"):
                    _accept(slot)
                    continue
                # Real servers refuse to translate from an unaccepted transcript.
                if not _is_accepted(node.get("automatic_google_transcription")):
                    raise HTTPException(status_code=400,
                                        detail="No transcription found")
                slot["_versions"].append(_version({**payload, "status": "in_progress"}))
            elif action == "automatic_bedrock_qual":
                quid = payload.get("uuid")
                if not quid:
                    raise HTTPException(status_code=400, detail="uuid is required")
                if not _is_accepted(node.get("automatic_google_transcription")):
                    raise HTTPException(status_code=400, detail="No transcription found")
                slot = node.setdefault(action, {}).setdefault(quid, {"_versions": []})
                slot["_versions"].append(_version({"uuid": quid, "status": "in_progress"}))
            else:
                raise HTTPException(status_code=400, detail=f"unknown action {action}")
    return doc


@app.get("/api/v2/assets/{uid}/data/")
def data(uid: str, limit: int = 100, start: int = 0,
         query: str = "", sort: str = "", fields: str = "", format: str = "json"):
    if start:
        return {"count": 3, "results": []}
    return {"count": 3, "results": [
        {"_id": i, "_uuid": f"sub-{i}", "meta/rootUuid": f"uuid:sub-{i}",
         "_submission_time": f"2026-08-1{i}T10:00:00"} for i in range(1, 4)
    ]}


@app.get("/api/v2/assets/{uid}/advanced_submission_post/")
def get_supplement(uid: str, submission: str = ""):
    _require_legacy_api()
    return STATE["supplements"].get(submission, {})


@app.post("/api/v2/assets/{uid}/advanced_submission_post/")
def post_supplement(uid: str, body: dict = Body(...)):
    _require_legacy_api()
    sub = body.get("submission")
    cur = STATE["supplements"].setdefault(sub, {"submission": sub})
    for xpath, actions in body.items():
        if xpath == "submission":
            continue
        node = cur.setdefault(xpath, {})
        # Simulate Kobo completing async jobs instantly.
        if "googlets" in actions:
            node["transcript"] = {"value": "Bonjour, la situation est difficile.",
                                  "languageCode": actions["googlets"].get("languageCode")}
            node["googlets"] = {**actions["googlets"], "status": "complete"}
        if "googletx" in actions:
            lang = actions["googletx"].get("languageCode")
            node.setdefault("translation", {})[lang] = {
                "value": "Hello, the situation is difficult.", "languageCode": lang}
            node["googletx"] = {**actions["googletx"], "status": "complete"}
        if "qual" in actions:
            node["qual"] = [
                {**q, "val": 3 if q.get("type") == "qualInteger" else "AI answer"}
                for q in copy.deepcopy(actions["qual"])
            ]
    return cur


@app.get("/api/v2/assets/{uid}/hooks/")
def list_hooks(uid: str):
    return {"count": len(STATE["hooks"]), "results": STATE["hooks"]}


@app.post("/api/v2/assets/{uid}/hooks/")
def create_hook(uid: str, body: dict = Body(...)):
    hook = {**body, "uid": "h" + uuid.uuid4().hex[:20], "success_count": 0, "failed_count": 0}
    STATE["hooks"].append(hook)
    return hook


@app.delete("/api/v2/assets/{uid}/hooks/{hook_uid}/")
def delete_hook(uid: str, hook_uid: str):
    STATE["hooks"][:] = [h for h in STATE["hooks"] if h["uid"] != hook_uid]
    return {}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8899, log_level="warning")
