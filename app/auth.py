"""Session-cookie auth for the admin UI.

Stateless signed cookie (HMAC-SHA256 over an expiry timestamp), keyed on the
admin password itself -- so changing ADMIN_PASSWORD invalidates every existing
session. No server-side session store to keep in sync between containers.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time

from fastapi import HTTPException, Request

from .config import settings

COOKIE_NAME = "autoqa_session"


def _key() -> bytes:
    return hashlib.sha256(("autoqa-session-v1:" + settings.admin_password).encode()).digest()


def issue_token(ttl_hours: int | None = None) -> tuple[str, int]:
    ttl = (ttl_hours if ttl_hours is not None else settings.admin_session_hours) * 3600
    expiry = int(time.time()) + ttl
    payload = str(expiry).encode()
    sig = hmac.new(_key(), payload, hashlib.sha256).hexdigest()
    return f"{expiry}.{sig}", ttl


def verify_token(token: str | None) -> bool:
    if not token or "." not in token:
        return False
    expiry_raw, _, sig = token.partition(".")
    try:
        expiry = int(expiry_raw)
    except ValueError:
        return False
    if expiry < time.time():
        return False
    expected = hmac.new(_key(), expiry_raw.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig)


def check_password(candidate: str) -> bool:
    if not settings.admin_password:
        return False
    return secrets.compare_digest(candidate.encode(), settings.admin_password.encode())


def require_admin(request: Request) -> None:
    """FastAPI dependency. Guards every /admin/api route."""
    if not settings.admin_password:
        raise HTTPException(
            status_code=503,
            detail="ADMIN_PASSWORD is not set; the admin UI is disabled.",
        )
    if not verify_token(request.cookies.get(COOKIE_NAME)):
        raise HTTPException(status_code=401, detail="not authenticated")
