"""FastAPI receiver for KoboToolbox REST Services.

Register one hook per asset pointing at:

    https://<host>/kobo/hook/<asset_uid>

with a custom header carrying WEBHOOK_SECRET. The endpoint only enqueues and
returns 200 immediately -- Kobo retries non-2xx responses, so we never do slow
work inside the request.
"""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse

from .admin import guarded as admin_guarded, router as admin_public
from .common import make_store, setup_logging, submission_uuid
from .config import settings
from .store import Store

setup_logging(settings.log_level)
log = logging.getLogger(__name__)

app = FastAPI(title="Kobo AutoQA pipeline", version="1.1.0", docs_url=None, redoc_url=None)
app.include_router(admin_public)
app.include_router(admin_guarded)

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
    return _store


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok", "jobs": store().stats()}


@app.post("/kobo/hook/{asset_uid}")
async def receive(
    asset_uid: str,
    request: Request,
    payload: dict = Body(...),
) -> dict:
    if settings.webhook_secret:
        provided = request.headers.get(settings.webhook_secret_header, "")
        if provided != settings.webhook_secret:
            log.warning("Rejected hook for %s: bad or missing secret header", asset_uid)
            raise HTTPException(status_code=403, detail="forbidden")

    allowed = set(settings.asset_uids) | set(store().watched_assets())
    if allowed and asset_uid not in allowed:
        log.warning("Rejected hook for unregistered asset %s", asset_uid)
        raise HTTPException(status_code=404, detail="unknown asset")

    uuid = submission_uuid(payload)
    if not uuid:
        log.error("Hook for %s had no usable submission uuid; keys=%s",
                  asset_uid, sorted(payload)[:25])
        # 200 on purpose: retrying will not help a malformed payload.
        return {"status": "ignored", "reason": "no submission uuid"}

    created = store().enqueue(asset_uid, uuid, {"source": "webhook"})
    log.info("Hook %s/%s -> %s", asset_uid, uuid, "queued" if created else "already known")
    return {"status": "queued" if created else "duplicate", "submission_uuid": uuid}
