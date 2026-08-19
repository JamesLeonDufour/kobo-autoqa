"""FastAPI receiver for KoboToolbox REST Services.

Register one hook per asset pointing at:

    https://<host>/kobo/hook/<asset_uid>

with a custom header carrying WEBHOOK_SECRET. The endpoint only enqueues and
returns 200 immediately -- Kobo retries non-2xx responses, so we never do slow
work inside the request.
"""
from __future__ import annotations

import logging
import secrets
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse

from . import runtime
from .admin import (admin_only as admin_admin, guarded as admin_guarded,
                    router as admin_public)
from .common import make_store, setup_logging, submission_uuid
from .config import configured, settings
from .store import Store

setup_logging(settings.log_level)
log = logging.getLogger(__name__)

# openapi_url=None as well as the doc UIs: the schema enumerated all 30
# endpoints, including every admin route, to anyone who asked.
app = FastAPI(title="Kobo AutoQA pipeline", version="1.1.0",
              docs_url=None, redoc_url=None, openapi_url=None)


# The admin UI is one self-contained page: no external scripts, styles, fonts
# or images, so it can be locked down hard. Inline script and style are its
# own, hence 'unsafe-inline' -- everything else is denied outright.
CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; "
       "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
       "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; "
       "form-action 'self'")

SECURITY_HEADERS = {
    # Browsers ignore HSTS served over plain HTTP, so sending it always is
    # safe and means the header survives a change of reverse proxy.
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
    "Content-Security-Policy": CSP,
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
}


@app.middleware("http")
async def security_headers(request, call_next):
    """Set the headers in the app rather than the proxy, so they travel with
    the deployment instead of depending on how it happens to be fronted."""
    response = await call_next(request)
    for header, value in SECURITY_HEADERS.items():
        response.headers.setdefault(header, value)
    return response
app.include_router(admin_public)
app.include_router(admin_guarded)
app.include_router(admin_admin)

STATIC = Path(__file__).parent / "static"
_store: Store | None = None


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse("/admin/")


@app.get("/admin/", include_in_schema=False)
@app.get("/admin", include_in_schema=False)
def admin_ui() -> FileResponse:
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})


def store() -> Store:
    global _store
    if _store is None:
        _store = make_store(settings)
        runtime.apply(_store, settings, force=True)
    return _store


@app.on_event("startup")
def _load_runtime_settings() -> None:
    """Pull UI-saved credentials into `settings` before serving traffic."""
    store()


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok", "jobs": store().stats()}


def _accept(owner: int | None, asset_uid: str, request: Request, payload: dict) -> dict:
    """Shared body for both hook routes: authenticate, then enqueue."""
    base = store()
    if owner is None:
        # Legacy single-tenant URL. Attribute it to whoever watches the asset;
        # if exactly one account does, that is unambiguous.
        owners = [u for u in base.active_user_ids()
                  if asset_uid in base.for_owner(u).watched_assets()]
        if len(owners) == 1:
            owner = owners[0]
        elif not owners:
            owner = 0  # pre-accounts data
        else:
            log.warning("Hook for %s matches %s accounts; use the per-account URL",
                        asset_uid, len(owners))
            raise HTTPException(status_code=409, detail="ambiguous asset; re-register the hook")

    s = runtime.for_owner(base, owner)
    if configured(s.webhook_secret):
        provided = request.headers.get(s.webhook_secret_header, "")
        if not secrets.compare_digest(provided, s.webhook_secret):
            log.warning("Rejected hook for %s: bad or missing secret header", asset_uid)
            raise HTTPException(status_code=403, detail="forbidden")
    elif s.webhook_secret:
        # The example secret from .env.example. Enforcing it would be theatre:
        # it is in the repository. Treat it as unset so the UI says plainly
        # that this endpoint is unauthenticated, rather than claiming it is
        # secured by a string everybody can read.
        log.error("WEBHOOK_SECRET is still the example value; this endpoint is "
                  "effectively unauthenticated. Set a real one.")

    owned = base.for_owner(owner)
    allowed = set(s.asset_uids) | set(owned.watched_assets())
    if allowed and asset_uid not in allowed:
        log.warning("Rejected hook for unregistered asset %s", asset_uid)
        raise HTTPException(status_code=404, detail="unknown asset")

    uuid = submission_uuid(payload)
    if not uuid:
        log.error("Hook for %s had no usable submission uuid; keys=%s",
                  asset_uid, sorted(payload)[:25])
        # 200 on purpose: retrying will not help a malformed payload.
        return {"status": "ignored", "reason": "no submission uuid"}

    created = owned.enqueue(asset_uid, uuid, {"source": "webhook"})
    log.info("Hook %s/%s (account %s) -> %s", asset_uid, uuid, owner,
             "queued" if created else "already known")
    return {"status": "queued" if created else "duplicate", "submission_uuid": uuid}


@app.post("/kobo/hook/{owner}/{asset_uid}")
async def receive_for_owner(owner: int, asset_uid: str, request: Request,
                            payload: dict = Body(...)) -> dict:
    """Per-account endpoint. The account id selects whose credentials apply."""
    return _accept(owner, asset_uid, request, payload)


@app.post("/kobo/hook/{asset_uid}")
async def receive(asset_uid: str, request: Request, payload: dict = Body(...)) -> dict:
    """Endpoint registered before accounts existed; still honoured."""
    return _accept(None, asset_uid, request, payload)
