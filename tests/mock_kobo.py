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

from fastapi import Body, FastAPI

app = FastAPI()

ASSET_UID = "aMOCK1234567890abcdef"

STATE = {
    "advanced_features": {},
    "hooks": [],
    "supplements": {},
}

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
    return {"ok": True}


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
    return SCHEMA


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
    return STATE["supplements"].get(submission, {})


@app.post("/api/v2/assets/{uid}/advanced_submission_post/")
def post_supplement(uid: str, body: dict = Body(...)):
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
