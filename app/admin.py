"""Admin JSON API backing the setup + monitoring UI."""
from __future__ import annotations

import logging
import uuid as uuidlib
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Response

from . import payloads as P
from .assetconf import defaults_from_env, resolve, save as save_cfg
from .auth import COOKIE_NAME, check_password, issue_token, require_admin
from .common import make_client, make_store, submission_uuid
from .config import settings
from .kobo import KoboError
from .store import STAGE_NEW, STAGE_FAILED

log = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/api")
guarded = APIRouter(prefix="/admin/api", dependencies=[Depends(require_admin)])

_store = None


def store():
    global _store
    if _store is None:
        _store = make_store(settings)
    return _store


def _client():
    try:
        return make_client(settings)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"Kobo client unavailable: {exc}") from exc


def _kobo_guard(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except KoboError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# auth (unguarded)
# ---------------------------------------------------------------------------
@router.post("/login")
def login(response: Response, payload: dict = Body(...)) -> dict:
    if not settings.admin_password:
        raise HTTPException(status_code=503, detail="ADMIN_PASSWORD is not set")
    if not check_password(str(payload.get("password", ""))):
        raise HTTPException(status_code=401, detail="Wrong password")
    token, ttl = issue_token()
    response.set_cookie(
        COOKIE_NAME, token, max_age=ttl, httponly=True,
        samesite="lax", secure=settings.admin_cookie_secure, path="/",
    )
    return {"ok": True}


@router.post("/logout")
def logout(response: Response) -> dict:
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"ok": True}


@router.get("/session")
def session() -> dict:
    return {"admin_enabled": bool(settings.admin_password)}


# ---------------------------------------------------------------------------
# environment / connection
# ---------------------------------------------------------------------------
@guarded.get("/env")
def env() -> dict:
    return {
        "kobo_url": settings.kobo_url,
        "token_set": bool(settings.kobo_token),
        "env_asset_uids": settings.asset_uids,
        "public_webhook_url": settings.public_webhook_url,
        "webhook_secret_set": bool(settings.webhook_secret),
        "webhook_secret_header": settings.webhook_secret_header,
        "dry_run": settings.dry_run,
        "poll_interval_seconds": settings.poll_interval_seconds,
        "async_poll_seconds": settings.async_poll_seconds,
        "max_attempts": settings.max_attempts,
        "defaults": defaults_from_env(settings, "").to_dict(),
    }


@guarded.get("/ping")
def ping() -> dict:
    with _client() as c:
        me = _kobo_guard(c._request, "GET", "/me/", params={"format": "json"})
    return {"ok": True, "username": (me or {}).get("username"), "server": settings.kobo_url}


# ---------------------------------------------------------------------------
# assets
# ---------------------------------------------------------------------------
@guarded.get("/assets")
def list_assets(q: str = "", limit: int = 200) -> dict:
    params: dict[str, Any] = {"format": "json", "limit": limit, "asset_type": "survey"}
    if q:
        params["q"] = q
    with _client() as c:
        data = _kobo_guard(c._request, "GET", "/api/v2/assets/", params=params) or {}
    saved = store().all_asset_settings()
    out = []
    for a in data.get("results", []):
        uid = a.get("uid")
        out.append({
            "uid": uid,
            "name": a.get("name"),
            "submissions": (a.get("deployment__submission_count") or 0),
            "deployed": bool(a.get("has_deployment")),
            "has_advanced": bool(a.get("advanced_features")),
            "managed": uid in saved,
            "enabled": saved.get(uid, {}).get("enabled", True) if uid in saved else False,
        })
    out.sort(key=lambda r: (not r["managed"], (r["name"] or "").lower()))
    return {"results": out, "count": len(out)}


@guarded.get("/assets/{asset_uid}")
def asset_detail(asset_uid: str) -> dict:
    with _client() as c:
        asset = _kobo_guard(c.get_asset, asset_uid)
        try:
            schema = c.get_advanced_submission_schema(asset_uid)
        except KoboError as exc:
            schema = {"_error": str(exc)}
        hooks = _kobo_guard(c.list_hooks, asset_uid)

    advanced = asset.get("advanced_features") or {}
    media = P.media_question_xpaths(asset)
    cfg = resolve(settings, store(), asset_uid)
    expected_endpoint = (
        f"{settings.public_webhook_url.rstrip('/')}/kobo/hook/{asset_uid}"
        if settings.public_webhook_url else ""
    )
    return {
        "uid": asset_uid,
        "name": asset.get("name"),
        "submission_count": ((asset.get("deployment__submission_count")) or 0),
        "media_questions": media,
        "configured_xpaths": P.configured_xpaths(advanced, []),
        "qual_survey": P.qual_survey(advanced),
        "advanced_features": advanced,
        "schema": schema,
        "detected_dialect": P.detect_dialect(schema),
        "config": cfg.to_dict(),
        "managed": asset_uid in store().all_asset_settings(),
        "hooks": [
            {"uid": h.get("uid"), "name": h.get("name"), "endpoint": h.get("endpoint"),
             "active": h.get("active"), "success_count": h.get("success_count"),
             "failed_count": h.get("failed_count"),
             "is_ours": h.get("endpoint") == expected_endpoint}
            for h in hooks
        ],
        "expected_endpoint": expected_endpoint,
    }


@guarded.put("/assets/{asset_uid}/config")
def save_config(asset_uid: str, patch: dict = Body(...)) -> dict:
    if isinstance(patch.get("translation_languages"), str):
        patch["translation_languages"] = [
            x.strip() for x in patch["translation_languages"].split(",") if x.strip()
        ]
    save_cfg(store(), asset_uid, patch)
    return {"ok": True, "config": resolve(settings, store(), asset_uid).to_dict()}


@guarded.delete("/assets/{asset_uid}/config")
def unmanage(asset_uid: str) -> dict:
    store().delete_asset_settings(asset_uid)
    return {"ok": True}


# ---------------------------------------------------------------------------
# qual survey (the preset analysis questions)
# ---------------------------------------------------------------------------
@guarded.put("/assets/{asset_uid}/qual")
def save_qual(asset_uid: str, payload: dict = Body(...)) -> dict:
    """Write the preset analysis questions into the asset's advanced_features."""
    survey = payload.get("qual_survey") or []
    xpaths = payload.get("xpaths") or []
    translations = payload.get("translation_languages") or []

    for q in survey:
        if not q.get("uuid"):
            q["uuid"] = str(uuidlib.uuid4())
        q.setdefault("scope", "by_question#survey")
        for choice in q.get("choices") or []:
            if not choice.get("uuid"):
                choice["uuid"] = str(uuidlib.uuid4())

    with _client() as c:
        asset = _kobo_guard(c.get_asset, asset_uid)
        advanced = asset.get("advanced_features") or {}
        if not xpaths:
            xpaths = P.media_question_xpaths(asset)
        if not xpaths:
            raise HTTPException(status_code=400, detail="No transcribable questions on this form")

        for q in survey:
            q.setdefault("xpath", xpaths[0])

        advanced.setdefault("transcript", {})["values"] = xpaths
        if translations:
            advanced.setdefault("translation", {})["values"] = xpaths
            advanced["translation"]["languages"] = translations
        advanced.setdefault("qual", {})["values"] = xpaths
        advanced["qual"]["qual_survey"] = survey

        if payload.get("dry_run"):
            return {"ok": True, "dry_run": True, "advanced_features": advanced}

        result = _kobo_guard(c.set_advanced_features, asset_uid, advanced)
    return {"ok": True, "advanced_features": result.get("advanced_features"),
            "qual_survey": survey}


# ---------------------------------------------------------------------------
# hooks
# ---------------------------------------------------------------------------
@guarded.post("/assets/{asset_uid}/hook")
def create_hook(asset_uid: str, payload: dict = Body(default={})) -> dict:
    base = payload.get("url") or settings.public_webhook_url
    if not base:
        raise HTTPException(status_code=400,
                            detail="Set PUBLIC_WEBHOOK_URL in .env or pass a url")
    endpoint = f"{base.rstrip('/')}/kobo/hook/{asset_uid}"
    headers = ({settings.webhook_secret_header: settings.webhook_secret}
               if settings.webhook_secret else {})
    with _client() as c:
        for h in _kobo_guard(c.list_hooks, asset_uid):
            if h.get("endpoint") == endpoint:
                return {"ok": True, "existing": True, "uid": h.get("uid"), "endpoint": endpoint}
        hook = _kobo_guard(c.create_hook, asset_uid, name=payload.get("name", "AutoQA pipeline"),
                           endpoint=endpoint, custom_headers=headers, subset_fields=[])
    return {"ok": True, "existing": False, "uid": hook.get("uid"), "endpoint": endpoint}


@guarded.delete("/assets/{asset_uid}/hook/{hook_uid}")
def delete_hook(asset_uid: str, hook_uid: str) -> dict:
    with _client() as c:
        _kobo_guard(c.delete_hook, asset_uid, hook_uid)
    return {"ok": True}


# ---------------------------------------------------------------------------
# jobs / monitoring
# ---------------------------------------------------------------------------
@guarded.get("/jobs")
def jobs(stage: str = "", limit: int = 100) -> dict:
    s = store()
    rows = s.list_jobs(stage or None, limit)
    return {
        "stats": s.stats(),
        "watched": s.watched_assets() or settings.asset_uids,
        "results": [
            {"asset_uid": r["asset_uid"], "submission_uuid": r["submission_uuid"],
             "stage": r["stage"], "attempts": r["attempts"],
             "next_attempt_at": r["next_attempt_at"], "last_error": r["last_error"],
             "updated_at": r["updated_at"], "created_at": r["created_at"]}
            for r in rows
        ],
    }


@guarded.post("/jobs/{asset_uid}/{sub_uuid}/retry")
def retry(asset_uid: str, sub_uuid: str) -> dict:
    store().enqueue(asset_uid, sub_uuid, {"source": "ui-retry"})
    store().reset(asset_uid, sub_uuid)
    return {"ok": True}


@guarded.post("/jobs/retry-failed")
def retry_failed() -> dict:
    s = store()
    n = 0
    for r in s.list_jobs(STAGE_FAILED, 1000):
        s.advance(r["asset_uid"], r["submission_uuid"], STAGE_NEW, delay=0, error=None)
        n += 1
    return {"ok": True, "requeued": n}


@guarded.get("/assets/{asset_uid}/submissions/{sub_uuid}")
def supplement(asset_uid: str, sub_uuid: str) -> dict:
    with _client() as c:
        return {"supplement": _kobo_guard(c.get_supplement, asset_uid, sub_uuid)}


@guarded.post("/assets/{asset_uid}/backfill")
def backfill(asset_uid: str, payload: dict = Body(default={})) -> dict:
    days = int(payload.get("days", 7))
    limit = int(payload.get("limit", 0))
    since = payload.get("since") or (
        datetime.now(timezone.utc) - timedelta(days=days)
    ).strftime("%Y-%m-%dT%H:%M:%S")
    s = store()
    added = 0
    try:
        with _client() as c:
            for sub in c.iter_submissions(
                asset_uid, query={"_submission_time": {"$gte": since}},
                sort={"_submission_time": 1},
                fields=["_id", "_uuid", "meta/rootUuid", "_submission_time"],
            ):
                u = submission_uuid(sub)
                if u and s.enqueue(asset_uid, u, {"source": "ui-backfill"}):
                    added += 1
                if limit and added >= limit:
                    break
    except KoboError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"ok": True, "enqueued": added, "since": since}
