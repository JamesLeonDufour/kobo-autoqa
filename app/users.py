"""User accounts: registration, approval, password hashing, sessions.

Accounts are created by anyone who can reach the sign-up form, but arrive
`pending` and cannot sign in until an admin approves them. That matters
because an active account can start billable transcription and analysis jobs.

The first account ever created is made an active admin -- someone has to be
able to approve the second one. `ADMIN_PASSWORD` keeps working as a break-glass
owner login so an existing deployment is never locked out.

Passwords are hashed with scrypt from the standard library; no extra
dependency, and far better than the single shared password it replaces.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import time

log = logging.getLogger(__name__)

STATUS_PENDING = "pending"
STATUS_ACTIVE = "active"
STATUS_DISABLED = "disabled"

# scrypt parameters. n=2**14 keeps a login around ~50ms on a small VPS, which
# is slow enough to make guessing expensive and fast enough to feel instant.
_N, _R, _P, _DKLEN = 2 ** 14, 8, 1, 32


# ---------------------------------------------------------------------------
# passwords
# ---------------------------------------------------------------------------
def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=_DKLEN)
    return f"scrypt${_N}${_R}${_P}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_hex, hash_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        dk = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex),
                            n=int(n), r=int(r), p=int(p), dklen=len(hash_hex) // 2)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(dk.hex(), hash_hex)


def password_problem(password: str) -> str | None:
    """Return why a password is unacceptable, or None if it is fine."""
    if len(password) < 12:
        return "Password must be at least 12 characters."
    if password.lower() in ("password1234", "changemenow", "123456789012"):
        return "That password is too easy to guess."
    return None


# ---------------------------------------------------------------------------
# sessions
# ---------------------------------------------------------------------------
def issue_token(store, user: dict, ttl_hours: int) -> tuple[str, int]:
    """Signed cookie: user id + expiry, keyed on the server secret *and* the
    user's password hash, so changing a password ends their other sessions."""
    ttl = ttl_hours * 3600
    expiry = int(time.time()) + ttl
    payload = f"{user['id']}.{expiry}"
    sig = hmac.new(_user_key(store, user), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}", ttl


def read_token(store, token: str | None) -> dict | None:
    """Return the signed-in user, or None. Rejects pending/disabled accounts."""
    if not token or token.count(".") != 2:
        return None
    uid_raw, expiry_raw, sig = token.split(".")
    try:
        uid, expiry = int(uid_raw), int(expiry_raw)
    except ValueError:
        return None
    if expiry < time.time():
        return None
    user = store.get_user_by_id(uid)
    if not user or user["status"] != STATUS_ACTIVE:
        return None
    expected = hmac.new(_user_key(store, user), f"{uid}.{expiry}".encode(),
                        hashlib.sha256).hexdigest()
    return user if hmac.compare_digest(expected, sig) else None


def _user_key(store, user: dict) -> bytes:
    return hashlib.sha256(
        b"autoqa-user-session-v1:" + store.session_secret().encode()
        + b":" + str(user["password_hash"]).encode()
    ).digest()


# ---------------------------------------------------------------------------
# registration / sign-in
# ---------------------------------------------------------------------------
def register(store, email: str, password: str, name: str = "") -> dict:
    """Create an account. The first one is an active admin, the rest pending."""
    email = (email or "").strip().lower()
    if "@" not in email or len(email) < 5:
        raise ValueError("Enter a valid email address.")
    problem = password_problem(password or "")
    if problem:
        raise ValueError(problem)
    if store.get_user_by_email(email):
        raise ValueError("An account with that email already exists.")

    first = store.count_users() == 0
    user_id = store.create_user(
        email=email, name=(name or "").strip(),
        password_hash=hash_password(password),
        status=STATUS_ACTIVE if first else STATUS_PENDING,
        is_admin=first,
    )
    log.info("Registered %s (%s)", email, "admin" if first else "pending approval")
    return store.get_user_by_id(user_id)


def authenticate(store, email: str, password: str) -> dict:
    """Return the user, or raise ValueError explaining why not."""
    user = store.get_user_by_email((email or "").strip().lower())
    # Hash anyway when the account is unknown, so a missing account and a wrong
    # password take about the same time and cannot be told apart.
    if not user:
        hash_password(password or "")
        raise ValueError("Email or password is incorrect.")
    if not verify_password(password or "", user["password_hash"]):
        raise ValueError("Email or password is incorrect.")
    if user["status"] == STATUS_PENDING:
        raise ValueError("Your account is waiting for an administrator to approve it.")
    if user["status"] == STATUS_DISABLED:
        raise ValueError("This account has been disabled.")
    store.touch_user_login(user["id"])
    return user


def public_view(user: dict) -> dict:
    return {
        "id": user["id"], "email": user["email"], "name": user["name"],
        "status": user["status"], "is_admin": bool(user["is_admin"]),
        "created_at": user["created_at"], "last_login_at": user["last_login_at"],
    }


def new_secret() -> str:
    return secrets.token_urlsafe(32)


# ---------------------------------------------------------------------------
# break-glass .env login
# ---------------------------------------------------------------------------
def issue_env_token(admin_password: str, ttl_hours: int) -> tuple[str, int]:
    ttl = ttl_hours * 3600
    expiry = int(time.time()) + ttl
    sig = hmac.new(_env_key(admin_password), str(expiry).encode(),
                   hashlib.sha256).hexdigest()
    return f"env.{expiry}.{sig}", ttl


def read_env_token(store, token: str, admin_password: str, ttl_hours: int) -> bool:
    try:
        _, expiry_raw, sig = token.split(".")
        expiry = int(expiry_raw)
    except ValueError:
        return False
    if expiry < time.time():
        return False
    expected = hmac.new(_env_key(admin_password), expiry_raw.encode(),
                        hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig)


def _env_key(admin_password: str) -> bytes:
    return hashlib.sha256(("autoqa-env-session-v1:" + admin_password).encode()).digest()
