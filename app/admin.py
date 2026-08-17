"""Admin JSON API backing the setup + monitoring UI.

Every route below operates on behalf of one signed-in account: its own Kobo
connection, its own forms, its own queue. The scoping lives in two objects
built per request -- an owner-bound Store and a Settings copy carrying that
user's credentials -- so an endpoint cannot accidentally read across accounts.
"""
from __future__ import annotations

import logging
import uuid as uuidlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Request, Response

from . import payloads as P
from . import runtime
from . import supplement as S
from . import users as U
from .assetconf import defaults_from_env, resolve, save as save_cfg
from .auth import (COOKIE_NAME, check_env_password, current_user, issue_for_user,
                   require_admin, store as base_store)
from .common import make_store, submission_uuid
from .config import Settings, settings
from .kobo import KoboClient, KoboError
from .store import STAGE_NEW, STAGE_FAILED, Store

log = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/api")                       # unauthenticated
guarded = APIRouter(prefix="/admin/api")                      # per-request auth
admin_only = APIRouter(prefix="/admin/api",
                       dependencies=[Depends(require_admin)])


# ---------------------------------------------------------------------------
# per-request context
# ---------------------------------------------------------------------------
@dataclass
class Ctx:
    user: dict
    store: Store        # limited to this user's rows
    settings: Settings  # this user's Kobo connection

    @property
    def uid(self) -> int:
        return int(self.user["id"])


def ctx(user: dict = Depends(current_user)) -> Ctx:
    base = base_store()
    uid = int(user["id"])
    return Ctx(user=user, store=base.for_owner(uid),
               settings=runtime.for_owner(base, uid))


def _client(c: Ctx) -> KoboClient:
    try:
        c.settings.validate()
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return KoboClient(c.settings.kobo_url, c.settings.kobo_token,
                      verify=c.settings.verify_tls, timeout=c.settings.http_timeout)


def _kobo_guard(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except KoboError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# accounts (unauthenticated)
# ---------------------------------------------------------------------------
@router.get("/session")
def session() -> dict:
    s = base_store()
    return {
        "admin_enabled": bool(settings.admin_password),
        "has_users": s.count_users() > 0,
        # The first account becomes an active admin; the rest wait for approval.
        "first_account_is_admin": s.count_users() == 0,
    }


@router.post("/register")
def register(response: Response, payload: dict = Body(...)) -> dict:
    s = base_store()
    try:
        user = U.register(s, payload.get("email", ""), payload.get("password", ""),
                          payload.get("name", ""))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if user["status"] == U.STATUS_ACTIVE:
        # First account: adopt anything configured before accounts existed, so
        # an upgraded deployment keeps its forms and queue.
        claimed = s.claim_unowned(user["id"])
        token, ttl = issue_for_user(user)
        _set_cookie(response, token, ttl)
        return {"ok": True, "signed_in": True, "claimed_rows": claimed,
                "user": U.public_view(user)}
    return {"ok": True, "signed_in": False,
            "message": "Account created. An administrator has to approve it "
                       "before you can sign in."}


@router.post("/login")
def login(response: Response, payload: dict = Body(...)) -> dict:
    s = base_store()
    email = (payload.get("email") or "").strip()
    password = str(payload.get("password", ""))

    # No email means the .env break-glass login.
    if not email:
        if not settings.admin_password:
            raise HTTPException(status_code=503, detail="ADMIN_PASSWORD is not set")
        if not check_env_password(password):
            raise HTTPException(status_code=401, detail="Wrong password")
        token, ttl = U.issue_env_token(settings.admin_password,
                                       settings.admin_session_hours)
        _set_cookie(response, token, ttl)
        return {"ok": True, "via": "env"}

    try:
        user = U.authenticate(s, email, password)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    token, ttl = issue_for_user(user)
    _set_cookie(response, token, ttl)
    return {"ok": True, "via": "account", "user": U.public_view(user)}


def _set_cookie(response: Response, token: str, ttl: int) -> None:
    response.set_cookie(COOKIE_NAME, token, max_age=ttl, httponly=True,
                        samesite="lax", secure=settings.admin_cookie_secure, path="/")


@router.post("/logout")
def logout(response: Response) -> dict:
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"ok": True}


@guarded.get("/me")
def me(c: Ctx = Depends(ctx)) -> dict:
    return {"user": U.public_view(c.user)}


@guarded.post("/me/password")
def change_password(payload: dict = Body(...), c: Ctx = Depends(ctx)) -> dict:
    if c.uid < 0:
        raise HTTPException(status_code=400,
                            detail="The .env login's password is changed in .env.")
    if not U.verify_password(str(payload.get("current", "")), c.user["password_hash"]):
        raise HTTPException(status_code=403, detail="Current password is incorrect.")
    new = str(payload.get("new", ""))
    problem = U.password_problem(new)
    if problem:
        raise HTTPException(status_code=400, detail=problem)
    base_store().set_user_password(c.uid, U.hash_password(new))
    # The cookie is keyed on the password hash, so this ends every session.
    return {"ok": True, "signed_out": True}


# ---------------------------------------------------------------------------
# user administration
# ---------------------------------------------------------------------------
@admin_only.get("/users")
def list_users() -> dict:
    return {"results": [U.public_view(u) for u in base_store().list_users()]}


@admin_only.post("/users/{user_id}/status")
def set_status(user_id: int, payload: dict = Body(...),
               admin: dict = Depends(require_admin)) -> dict:
    status = payload.get("status")
    if status not in (U.STATUS_ACTIVE, U.STATUS_PENDING, U.STATUS_DISABLED):
        raise HTTPException(status_code=400, detail="Unknown status.")
    s = base_store()
    target = s.get_user_by_id(user_id)
    if not target:
        raise HTTPException(status_code=404, detail="No such account.")
    if target["id"] == admin.get("id") and status != U.STATUS_ACTIVE:
        raise HTTPException(status_code=400,
                            detail="You cannot disable your own account.")
    s.set_user_status(user_id, status)
    return {"ok": True, "user": U.public_view(s.get_user_by_id(user_id))}


@admin_only.post("/users/{user_id}/admin")
def set_admin(user_id: int, payload: dict = Body(...),
              admin: dict = Depends(require_admin)) -> dict:
    s = base_store()
    make_admin = bool(payload.get("is_admin"))
    if not make_admin and user_id == admin.get("id"):
        raise HTTPException(status_code=400,
                            detail="You cannot remove your own admin access.")
    if not make_admin and sum(1 for u in s.list_users() if u["is_admin"]) <= 1:
        raise HTTPException(status_code=400,
                            detail="At least one administrator must remain.")
    s.set_user_admin(user_id, make_admin)
    return {"ok": True}


@admin_only.delete("/users/{user_id}")
def delete_user(user_id: int, admin: dict = Depends(require_admin)) -> dict:
    if user_id == admin.get("id"):
        raise HTTPException(status_code=400, detail="You cannot delete your own account.")
    s = base_store()
    if not s.get_user_by_id(user_id):
        raise HTTPException(status_code=404, detail="No such account.")
    s.delete_user(user_id)
    return {"ok": True}


# ---------------------------------------------------------------------------
# environment / connection
# ---------------------------------------------------------------------------
@guarded.get("/env")
def env(c: Ctx = Depends(ctx)) -> dict:
    return {
        "kobo_url": c.settings.kobo_url,
        "token_set": bool(c.settings.kobo_token),
        "env_asset_uids": c.settings.asset_uids,
        "public_webhook_url": c.settings.public_webhook_url,
        "webhook_secret_set": bool(c.settings.webhook_secret),
        "webhook_secret_header": c.settings.webhook_secret_header,
        "dry_run": c.settings.dry_run,
        "poll_interval_seconds": c.settings.poll_interval_seconds,
        "async_poll_seconds": c.settings.async_poll_seconds,
        "max_attempts": c.settings.max_attempts,
        "defaults": defaults_from_env(c.settings, "").to_dict(),
        "user": U.public_view(c.user),
    }


@guarded.get("/ping")
def ping(c: Ctx = Depends(ctx)) -> dict:
    with _client(c) as client:
        me_ = _kobo_guard(client._request, "GET", "/me/", params={"format": "json"})
    return {"ok": True, "username": (me_ or {}).get("username"),
            "server": c.settings.kobo_url}


@guarded.get("/credentials")
def get_credentials(c: Ctx = Depends(ctx)) -> dict:
    return {"credentials": runtime.describe_for_owner(base_store(), c.uid, c.settings),
            "env_defaults": {k: ("" if k in runtime.SECRET_FIELDS else v)
                             for k, v in runtime.env_baseline().items()}}


@guarded.put("/credentials")
def put_credentials(patch: dict = Body(...), c: Ctx = Depends(ctx)) -> dict:
    unknown = sorted(set(patch) - set(runtime.FIELDS))
    if unknown:
        raise HTTPException(status_code=400,
                            detail=f"Not editable here: {', '.join(unknown)}")
    url = str(patch.get("kobo_url", c.settings.kobo_url) or "").strip()
    if url and not url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400,
                            detail="Server URL must start with http:// or https://")
    base = base_store()
    runtime.save_for_owner(base, c.uid, patch)
    fresh = runtime.for_owner(base, c.uid)
    return {"ok": True, "credentials": runtime.describe_for_owner(base, c.uid, fresh)}


@guarded.delete("/credentials")
def reset_credentials(c: Ctx = Depends(ctx)) -> dict:
    base = base_store()
    runtime.clear_for_owner(base, c.uid)
    fresh = runtime.for_owner(base, c.uid)
    return {"ok": True, "credentials": runtime.describe_for_owner(base, c.uid, fresh)}


@guarded.post("/credentials/test")
def test_credentials(payload: dict = Body(default={}), c: Ctx = Depends(ctx)) -> dict:
    url = str(payload.get("kobo_url") or c.settings.kobo_url or "").strip().rstrip("/")
    token = str(payload.get("kobo_token") or "").strip() or c.settings.kobo_token
    verify = payload.get("verify_tls")
    verify = c.settings.verify_tls if verify is None else bool(verify)

    if not url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="Server URL must start with http(s)://")
    if not token:
        raise HTTPException(status_code=400, detail="No API token to test")

    client = KoboClient(url, token, verify=verify, timeout=c.settings.http_timeout)
    try:
        me_ = client._request("GET", "/me/", params={"format": "json"}) or {}
    except KoboError as exc:
        detail = ("Token rejected by the server (401/403)."
                  if exc.status in (401, 403) else str(exc))
        raise HTTPException(status_code=502, detail=detail) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Cannot reach {url}: {exc}") from exc
    finally:
        client.close()
    return {"ok": True, "username": me_.get("username"), "email": me_.get("email"),
            "server": url}


# ---------------------------------------------------------------------------
# assets
# ---------------------------------------------------------------------------
@guarded.get("/assets")
def list_assets(q: str = "", limit: int = 200, c: Ctx = Depends(ctx)) -> dict:
    params: dict[str, Any] = {"format": "json", "limit": limit, "asset_type": "survey"}
    if q:
        params["q"] = q
    with _client(c) as client:
        data = _kobo_guard(client._request, "GET", "/api/v2/assets/", params=params) or {}
    saved = c.store.all_asset_settings()
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


def _hook_view(c: Ctx, asset_uid: str, hooks: list[dict]) -> tuple[list[dict], str]:
    expected = (f"{c.settings.public_webhook_url.rstrip('/')}/kobo/hook/{c.uid}/{asset_uid}"
                if c.settings.public_webhook_url else "")
    legacy = (f"{c.settings.public_webhook_url.rstrip('/')}/kobo/hook/{asset_uid}"
              if c.settings.public_webhook_url else "")
    return ([{"uid": h.get("uid"), "name": h.get("name"), "endpoint": h.get("endpoint"),
              "active": h.get("active"), "success_count": h.get("success_count"),
              "failed_count": h.get("failed_count"),
              "is_ours": h.get("endpoint") in (expected, legacy)}
             for h in hooks], expected)


@guarded.get("/assets/{asset_uid}")
def asset_detail(asset_uid: str, c: Ctx = Depends(ctx)) -> dict:
    features: S.AssetFeatures | None = None
    schema: dict = {}
    with _client(c) as client:
        asset = _kobo_guard(client.get_asset, asset_uid)
        try:
            features = S.AssetFeatures(client.list_advanced_features(asset_uid))
        except KoboError:
            features = None
        if features is None:
            try:
                schema = client.get_advanced_submission_schema(asset_uid)
            except KoboError as exc:
                schema = {"_error": str(exc)}
        hooks = _kobo_guard(client.list_hooks, asset_uid)

    hook_rows, expected = _hook_view(c, asset_uid, hooks)
    cfg = resolve(c.settings, c.store, asset_uid)
    media = P.media_question_xpaths(asset)

    if features is not None:
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
            "uid": asset_uid, "name": asset.get("name"),
            "submission_count": asset.get("deployment__submission_count") or 0,
            "media_questions": media,
            "configured_xpaths": sorted(per_question),
            "detected_dialect": P.SUPPLEMENT,
            "supports_hints": True,
            "per_question": per_question,
            "qual_survey": [], "advanced_features": {}, "schema": {},
            "config": cfg.to_dict(),
            "managed": asset_uid in c.store.all_asset_settings(),
            "hooks": hook_rows, "expected_endpoint": expected,
        }

    advanced = asset.get("advanced_features") or {}
    return {
        "uid": asset_uid, "name": asset.get("name"),
        "submission_count": asset.get("deployment__submission_count") or 0,
        "media_questions": media,
        "configured_xpaths": P.configured_xpaths(advanced, []),
        "qual_survey": P.qual_survey(advanced),
        "advanced_features": advanced,
        "schema": schema,
        "detected_dialect": P.detect_dialect(schema),
        "config": cfg.to_dict(),
        "managed": asset_uid in c.store.all_asset_settings(),
        "hooks": hook_rows, "expected_endpoint": expected,
    }


@guarded.put("/assets/{asset_uid}/config")
def save_config(asset_uid: str, patch: dict = Body(...), c: Ctx = Depends(ctx)) -> dict:
    if isinstance(patch.get("translation_languages"), str):
        patch["translation_languages"] = [
            x.strip() for x in patch["translation_languages"].split(",") if x.strip()
        ]
    save_cfg(c.store, asset_uid, patch)
    return {"ok": True, "config": resolve(c.settings, c.store, asset_uid).to_dict()}


@guarded.delete("/assets/{asset_uid}/config")
def unmanage(asset_uid: str, c: Ctx = Depends(ctx)) -> dict:
    c.store.delete_asset_settings(asset_uid)
    return {"ok": True}


# ---------------------------------------------------------------------------
# analysis questions
# ---------------------------------------------------------------------------
@guarded.put("/assets/{asset_uid}/qual")
def save_qual(asset_uid: str, payload: dict = Body(...), c: Ctx = Depends(ctx)) -> dict:
    """Write the analysis questions into the form's configuration."""
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

    with _client(c) as client:
        try:
            features = S.AssetFeatures(client.list_advanced_features(asset_uid))
        except KoboError:
            features = None
        if features is not None:
            return _save_qual_supplement(client, asset_uid, features, payload, survey)

        asset = _kobo_guard(client.get_asset, asset_uid)
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

        result = _kobo_guard(client.set_advanced_features, asset_uid, advanced)
    return {"ok": True, "advanced_features": result.get("advanced_features"),
            "qual_survey": survey}


def _save_qual_supplement(client, asset_uid: str, features: S.AssetFeatures,
                          payload: dict, survey: list[dict]) -> dict:
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

    edited = payload.get("edit_xpath") or xpaths[0]
    stored: dict[str, list[dict]] = {}
    try:
        for xpath in xpaths:
            stored[xpath] = S.sync_question(
                client, asset_uid, features, xpath,
                transcribe_language=payload.get("transcript_language") or "",
                translate_languages=payload.get("translation_languages") or [],
                questions=survey,
                enable_qual=payload.get("enable_qual", True),
                merge=(xpath != edited),
            )
            features = S.AssetFeatures(client.list_advanced_features(asset_uid))
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


# ---------------------------------------------------------------------------
# hooks
# ---------------------------------------------------------------------------
@guarded.post("/assets/{asset_uid}/hook")
def create_hook(asset_uid: str, payload: dict = Body(default={}),
                c: Ctx = Depends(ctx)) -> dict:
    base = payload.get("url") or c.settings.public_webhook_url
    if not base:
        raise HTTPException(status_code=400,
                            detail="Set the public URL on the Connection tab first.")
    # The account id is in the path so the receiver knows whose credentials to
    # use without having to guess from the asset.
    endpoint = f"{base.rstrip('/')}/kobo/hook/{c.uid}/{asset_uid}"
    headers = ({c.settings.webhook_secret_header: c.settings.webhook_secret}
               if c.settings.webhook_secret else {})
    with _client(c) as client:
        for h in _kobo_guard(client.list_hooks, asset_uid):
            if h.get("endpoint") == endpoint:
                return {"ok": True, "existing": True, "uid": h.get("uid"),
                        "endpoint": endpoint}
        hook = _kobo_guard(client.create_hook, asset_uid,
                           name=payload.get("name", "AutoQA pipeline"),
                           endpoint=endpoint, custom_headers=headers, subset_fields=[])
    return {"ok": True, "existing": False, "uid": hook.get("uid"), "endpoint": endpoint}


@guarded.delete("/assets/{asset_uid}/hook/{hook_uid}")
def delete_hook(asset_uid: str, hook_uid: str, c: Ctx = Depends(ctx)) -> dict:
    with _client(c) as client:
        _kobo_guard(client.delete_hook, asset_uid, hook_uid)
    return {"ok": True}


# ---------------------------------------------------------------------------
# jobs / monitoring
# ---------------------------------------------------------------------------
@guarded.get("/jobs")
def jobs(stage: str = "", limit: int = 100, c: Ctx = Depends(ctx)) -> dict:
    rows = c.store.list_jobs(stage or None, limit)
    return {
        "stats": c.store.stats(),
        "watched": c.store.watched_assets() or c.settings.asset_uids,
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
def retry(asset_uid: str, sub_uuid: str, c: Ctx = Depends(ctx)) -> dict:
    c.store.enqueue(asset_uid, sub_uuid, {"source": "ui-retry"})
    c.store.reset(asset_uid, sub_uuid)
    return {"ok": True}


@guarded.delete("/jobs/{asset_uid}/{sub_uuid}")
def delete_job(asset_uid: str, sub_uuid: str, c: Ctx = Depends(ctx)) -> dict:
    """Drop a queue entry. Only removes it here — Kobo is not touched."""
    return {"ok": True, "deleted": c.store.delete_job(asset_uid, sub_uuid)}


@guarded.post("/jobs/retry-failed")
def retry_failed(c: Ctx = Depends(ctx)) -> dict:
    n = 0
    for r in c.store.list_jobs(STAGE_FAILED, 1000):
        c.store.advance(r["asset_uid"], r["submission_uuid"], STAGE_NEW,
                        delay=0, error=None)
        n += 1
    return {"ok": True, "requeued": n}


@guarded.get("/assets/{asset_uid}/submissions/{sub_uuid}")
def supplement(asset_uid: str, sub_uuid: str, c: Ctx = Depends(ctx)) -> dict:
    """Whatever NLP results Kobo currently holds for one submission.

    Which endpoint holds them depends on the server's dialect, and the older
    one 404s on a current server -- returning an empty document rather than an
    error, which looks like "no results" instead of "wrong endpoint".
    """
    with _client(c) as client:
        try:
            client.list_advanced_features(asset_uid)
        except KoboError:
            return {"supplement": _kobo_guard(client.get_supplement, asset_uid, sub_uuid),
                    "api": "advanced_submission_post"}
        try:
            data = client.get_data_supplement(asset_uid, sub_uuid, missing_ok=False)
        except KoboError as exc:
            if exc.status == 404:
                raise HTTPException(
                    status_code=404,
                    detail=f"KoboToolbox has no submission with uuid {sub_uuid} on "
                           f"this form. The queue entry is stale — delete it.",
                ) from exc
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return {"supplement": data, "api": "supplement"}


@guarded.post("/assets/{asset_uid}/backfill")
def backfill(asset_uid: str, payload: dict = Body(default={}),
             c: Ctx = Depends(ctx)) -> dict:
    days = int(payload.get("days", 7))
    limit = int(payload.get("limit", 0))
    since = payload.get("since") or (
        datetime.now(timezone.utc) - timedelta(days=days)
    ).strftime("%Y-%m-%dT%H:%M:%S")
    added = 0
    try:
        with _client(c) as client:
            for sub in client.iter_submissions(
                asset_uid, query={"_submission_time": {"$gte": since}},
                sort={"_submission_time": 1},
                fields=["_id", "_uuid", "meta/rootUuid", "_submission_time"],
            ):
                u = submission_uuid(sub)
                if u and c.store.enqueue(asset_uid, u, {"source": "ui-backfill"}):
                    added += 1
                if limit and added >= limit:
                    break
    except KoboError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"ok": True, "enqueued": added, "since": since}
