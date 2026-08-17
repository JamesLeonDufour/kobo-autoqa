"""Admin JSON API backing the setup + monitoring UI."""
from __future__ import annotations

import logging
import uuid as uuidlib
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Response

from . import payloads as P
from . import runtime
from . import supplement as S
from .assetconf import defaults_from_env, resolve, save as save_cfg
from .auth import COOKIE_NAME, check_password, issue_token, require_admin
from .common import make_client, make_store, submission_uuid
from .config import settings
from .kobo import KoboClient, KoboError
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
        return make_client(settings, store())
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc


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
    runtime.apply(store(), settings)
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
# connection credentials (editable from the UI, stored in SQLite)
# ---------------------------------------------------------------------------
@guarded.get("/credentials")
def get_credentials() -> dict:
    runtime.apply(store(), settings)
    return {"credentials": runtime.describe(store(), settings),
            "env_defaults": {
                k: ("" if k in runtime.SECRET_FIELDS else v)
                for k, v in runtime.env_baseline().items()
            }}


@guarded.put("/credentials")
def put_credentials(patch: dict = Body(...)) -> dict:
    unknown = sorted(set(patch) - set(runtime.FIELDS))
    if unknown:
        raise HTTPException(status_code=400,
                            detail=f"Not editable here: {', '.join(unknown)}")
    url = str(patch.get("kobo_url", settings.kobo_url) or "").strip()
    if url and not url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400,
                            detail="Server URL must start with http:// or https://")
    runtime.save(store(), patch, settings)
    return {"ok": True, "credentials": runtime.describe(store(), settings)}


@guarded.delete("/credentials")
def reset_credentials() -> dict:
    """Forget every UI-entered value; .env takes over again."""
    runtime.clear(store(), settings)
    return {"ok": True, "credentials": runtime.describe(store(), settings)}


@guarded.post("/credentials/test")
def test_credentials(payload: dict = Body(default={})) -> dict:
    """Try a candidate URL + token without saving anything."""
    url = str(payload.get("kobo_url") or settings.kobo_url or "").strip().rstrip("/")
    token = str(payload.get("kobo_token") or "").strip() or settings.kobo_token
    verify = payload.get("verify_tls")
    verify = settings.verify_tls if verify is None else bool(verify)

    if not url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="Server URL must start with http(s)://")
    if not token:
        raise HTTPException(status_code=400, detail="No API token to test")

    client = KoboClient(url, token, verify=verify, timeout=settings.http_timeout)
    try:
        me = client._request("GET", "/me/", params={"format": "json"}) or {}
    except KoboError as exc:
        detail = ("Token rejected by the server (401/403)."
                  if exc.status in (401, 403) else str(exc))
        raise HTTPException(status_code=502, detail=detail) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Cannot reach {url}: {exc}") from exc
    finally:
        client.close()
    return {"ok": True, "username": me.get("username"), "email": me.get("email"), "server": url}


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
    features: S.AssetFeatures | None = None
    schema: dict = {}
    with _client() as c:
        asset = _kobo_guard(c.get_asset, asset_uid)
        # Current servers expose /advanced-features/; only fall back to
        # sniffing the old submission schema when they do not.
        try:
            features = S.AssetFeatures(c.list_advanced_features(asset_uid))
        except KoboError:
            features = None
        if features is None:
            try:
                schema = c.get_advanced_submission_schema(asset_uid)
            except KoboError as exc:
                schema = {"_error": str(exc)}
        hooks = _kobo_guard(c.list_hooks, asset_uid)

    advanced = asset.get("advanced_features") or {}
    if features is not None:
        return _supplement_detail(asset_uid, asset, features, hooks)
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


def _hook_view(asset_uid: str, hooks: list[dict]) -> tuple[list[dict], str]:
    expected = (f"{settings.public_webhook_url.rstrip('/')}/kobo/hook/{asset_uid}"
                if settings.public_webhook_url else "")
    return ([{"uid": h.get("uid"), "name": h.get("name"), "endpoint": h.get("endpoint"),
              "active": h.get("active"), "success_count": h.get("success_count"),
              "failed_count": h.get("failed_count"),
              "is_ours": h.get("endpoint") == expected}
             for h in hooks], expected)


def _supplement_detail(asset_uid: str, asset: dict, features: S.AssetFeatures,
                       hooks: list[dict]) -> dict:
    """Asset detail for a server speaking the current NLP API.

    Configuration here is per audio question, so the response is shaped that
    way: the UI picks one recording and edits the settings that belong to it.
    """
    media = P.media_question_xpaths(asset)
    cfg = resolve(settings, store(), asset_uid)
    hook_rows, expected = _hook_view(asset_uid, hooks)
    per_question = {
        x: {
            "transcript_language": features.transcribe.get(x, ""),
            "translation_languages": features.translate.get(x, []),
            "qual_survey": features.definitions.get(x, []),
            "auto_qual_uuids": features.qual.get(x, []),
            "configured": bool(features.transcribe.get(x) or features.definitions.get(x)),
        }
        for x in sorted(set(media) | set(features.xpaths) | set(features.definitions))
    }
    return {
        "uid": asset_uid,
        "name": asset.get("name"),
        "submission_count": asset.get("deployment__submission_count") or 0,
        "media_questions": media,
        "configured_xpaths": sorted(per_question),
        "detected_dialect": P.SUPPLEMENT,
        "supports_hints": True,
        "per_question": per_question,
        # Kept for the older UI paths; meaningless on this dialect.
        "qual_survey": [],
        "advanced_features": {},
        "schema": {},
        "config": cfg.to_dict(),
        "managed": asset_uid in store().all_asset_settings(),
        "hooks": hook_rows,
        "expected_endpoint": expected,
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
def _save_qual_supplement(c, asset_uid: str, features: S.AssetFeatures,
                          payload: dict, survey: list[dict]) -> dict:
    """Write one audio question's configuration through the current API."""
    xpaths = payload.get("xpaths") or []
    if not xpaths:
        raise HTTPException(status_code=400,
                            detail="Select at least one audio question to configure.")

    try:
        definitions = S.qual_definitions(survey)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    skipped = [q["labels"]["_default"] for q in definitions
               if q["type"] not in S.AUTO_QUAL_TYPES]

    if payload.get("dry_run"):
        return {"ok": True, "dry_run": True, "xpaths": xpaths,
                "manual_qual": definitions,
                "automatic_bedrock_qual": S.auto_qual_params(survey),
                "not_auto_answerable": skipped}

    # Each recording gets its own copy: analysis answers are stored per
    # recording under the question uuid, so they must not be shared.
    # The recording open in the editor is replaced outright, so removing a
    # question there takes effect. The others are merged into, so applying to
    # several never strips questions that belong to only one of them.
    edited = payload.get("edit_xpath") or xpaths[0]
    stored: dict[str, list[dict]] = {}
    try:
        for xpath in xpaths:
            stored[xpath] = S.sync_question(
                c, asset_uid, features, xpath,
                transcribe_language=payload.get("transcript_language") or "",
                translate_languages=payload.get("translation_languages") or [],
                questions=survey,
                enable_qual=payload.get("enable_qual", True),
                merge=(xpath != edited),
            )
            # Re-read so the next recording sees the rows just written.
            features = S.AssetFeatures(c.list_advanced_features(asset_uid))
    except KoboError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Applied {len(stored)} of {len(xpaths)} recordings, then: {exc}",
        ) from exc

    first = xpaths[0]
    return {"ok": True, "xpaths": xpaths, "xpath": first,
            "qual_survey": features.definitions.get(first, []),
            "auto_qual_uuids": features.qual.get(first, []),
            "transcript_language": features.transcribe.get(first, ""),
            "translation_languages": features.translate.get(first, []),
            "not_auto_answerable": skipped}


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
        # Current servers store this per audio question through a different
        # endpoint; writing the legacy blob there would be silently ignored.
        try:
            features = S.AssetFeatures(c.list_advanced_features(asset_uid))
        except KoboError:
            features = None
        if features is not None:
            return _save_qual_supplement(c, asset_uid, features, payload, survey)

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
             "note": (r["note"] if "note" in r.keys() else None),
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
