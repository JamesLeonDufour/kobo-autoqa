"""Session handling for the admin UI.

Two ways in, and the second exists only so an existing deployment is never
locked out:

  * a user account (see app/users.py) -- the normal path;
  * the `ADMIN_PASSWORD` from .env, which signs in as the owner account. It is
    the break-glass login, and on a fresh database it is what lets the first
    person in before any account exists.

Session cookies are signed, never stored server-side, and keyed on the user's
password hash so changing a password ends that user's other sessions.
"""
from __future__ import annotations

import secrets

from fastapi import Depends, HTTPException, Request

from . import users as U
from .common import make_store
from .config import settings
from .store import Store

COOKIE_NAME = "autoqa_session"
OWNER_SENTINEL = 0   # the .env break-glass login shares the pre-accounts bucket


def store() -> Store:
    return make_store(settings)


def check_env_password(candidate: str) -> bool:
    if not settings.admin_password:
        return False
    return secrets.compare_digest(candidate.encode(), settings.admin_password.encode())


def issue_for_user(user: dict) -> tuple[str, int]:
    return U.issue_token(store(), user, settings.admin_session_hours)


def current_user(request: Request) -> dict:
    """FastAPI dependency: the signed-in account, or 401.

    The break-glass login resolves to the first admin account when one exists,
    so its actions are attributed to a real owner rather than to nothing.
    """
    token = request.cookies.get(COOKIE_NAME)
    s = store()
    user = U.read_token(s, token)
    if user:
        return user
    if token and token.startswith("env."):
        if not settings.admin_password:
            raise HTTPException(status_code=401, detail="not authenticated")
        if not U.read_env_token(s, token, settings.admin_password,
                               settings.admin_session_hours):
            raise HTTPException(status_code=401, detail="not authenticated")
        admin = next((u for u in s.list_users() if u["is_admin"]), None)
        return admin or {
            "id": OWNER_SENTINEL, "email": ".env owner", "name": "",
            "status": "active", "is_admin": 1, "password_hash": "",
            "created_at": 0, "last_login_at": None,
        }
    raise HTTPException(status_code=401, detail="not authenticated")


def require_admin(user: dict = Depends(current_user)) -> dict:
    """Guards routes that manage other people's accounts."""
    if not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="Administrator access required.")
    return user


def owned_store(user: dict = Depends(current_user)) -> Store:
    """A store limited to the signed-in user's rows."""
    return store().for_owner(int(user["id"]))
