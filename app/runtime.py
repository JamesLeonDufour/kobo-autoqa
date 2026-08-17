"""Runtime connection settings that the admin UI can edit.

Everything in `.env` stays a valid way to configure the pipeline. This module
adds a second, higher-precedence source: a row in SQLite (`app_settings`, key
`connection`) written from the admin UI's Connection tab. On startup -- and
again whenever the row changes -- the stored values are applied onto the
process-wide `settings` object, so the rest of the codebase keeps reading
`settings.kobo_token` and friends without knowing where the value came from.

Precedence, highest first:

    1. values saved in the admin UI
    2. environment / .env
    3. the defaults in config.Settings

ADMIN_PASSWORD is deliberately *not* editable here. It is the credential that
guards this very screen, so it has to come from the environment -- otherwise a
fresh deployment would have no way to authenticate the first login.
"""
from __future__ import annotations

import logging
from dataclasses import replace

from .config import Settings, settings
from .store import Store

log = logging.getLogger(__name__)

KEY = "connection"

# Editable from the UI. Maps field name -> coercion applied to incoming JSON.
FIELDS: dict[str, type] = {
    "kobo_url": str,
    "kobo_token": str,
    "verify_tls": bool,
    "public_webhook_url": str,
    "webhook_secret": str,
    "webhook_secret_header": str,
}

SECRET_FIELDS = {"kobo_token", "webhook_secret"}

# Whatever the environment gave us at import time, before any UI override is
# applied. Kept so "revert to .env" can restore the original values.
_ENV_BASELINE: dict[str, object] = {f: getattr(settings, f) for f in FIELDS}

_applied_at: float = -1.0


def env_baseline() -> dict:
    return dict(_ENV_BASELINE)


def _coerce(field: str, value: object) -> object:
    kind = FIELDS[field]
    if kind is bool:
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("1", "true", "yes", "on")
    text = str(value if value is not None else "").strip()
    if field == "kobo_url":
        text = text.rstrip("/")
    return text


def key_for(owner: int | None) -> str:
    """Each account keeps its own connection; owner None is the legacy row."""
    return KEY if owner is None else f"{KEY}:{owner}"


def stored(store: Store, owner: int | None = None) -> dict:
    """The raw overrides currently saved in the database."""
    return {k: v for k, v in (store.get_app_settings(key_for(owner)) or {}).items()
            if k in FIELDS}


def for_owner(store: Store, owner: int, base: Settings = settings) -> Settings:
    """A Settings copy carrying one user's Kobo connection.

    Returns a copy rather than mutating the process-wide settings, because
    several users' jobs are handled in the same process and must not see each
    other's credentials.
    """
    resolved = replace(base)
    saved = stored(store, owner)
    for field in FIELDS:
        value = saved.get(field)
        if value is None or (isinstance(value, str) and not value.strip()):
            value = _ENV_BASELINE[field]
        setattr(resolved, field, value)
    return resolved


def save_for_owner(store: Store, owner: int, patch: dict) -> dict:
    current = stored(store, owner)
    for field, value in patch.items():
        if field not in FIELDS:
            continue
        coerced = _coerce(field, value)
        if isinstance(coerced, str) and not coerced:
            current.pop(field, None)
        else:
            current[field] = coerced
    store.set_app_settings(key_for(owner), current)
    return current


def clear_for_owner(store: Store, owner: int) -> None:
    store.delete_app_settings(key_for(owner))


def describe_for_owner(store: Store, owner: int, resolved: Settings) -> dict:
    saved = stored(store, owner)
    out: dict[str, object] = {}
    for field in FIELDS:
        effective = getattr(resolved, field)
        source = "ui" if field in saved else ("env" if _ENV_BASELINE[field] else "default")
        if field in SECRET_FIELDS:
            out[field] = {"set": bool(effective), "hint": _hint(str(effective)),
                          "source": source}
        else:
            out[field] = {"value": effective, "source": source}
    out["admin_password_from_env"] = True
    return out


def apply(store: Store, s: Settings = settings, *, force: bool = False) -> bool:
    """Push stored overrides onto `s`. Returns True when values changed.

    Cheap enough to call on every worker tick: it only touches the database
    when the stored row's timestamp has moved since the last apply.
    """
    global _applied_at
    stamp = store.app_settings_updated_at(KEY)
    if not force and stamp == _applied_at:
        return False

    saved = stored(store)
    for field in FIELDS:
        value = saved.get(field)
        # An empty string means "not set here" -- fall back to the environment
        # rather than blanking a working .env value.
        if value is None or (isinstance(value, str) and not value.strip()):
            value = _ENV_BASELINE[field]
        setattr(s, field, value)

    _applied_at = stamp
    if saved:
        log.info("Applied UI connection settings: %s", ", ".join(sorted(saved)))
    return True


def save(store: Store, patch: dict, s: Settings = settings) -> dict:
    """Merge a patch into the stored overrides and apply it immediately."""
    current = stored(store)
    for field, value in patch.items():
        if field not in FIELDS:
            continue
        coerced = _coerce(field, value)
        # Blanking a string field removes the override (back to .env).
        if isinstance(coerced, str) and not coerced:
            current.pop(field, None)
        else:
            current[field] = coerced
    store.set_app_settings(KEY, current)
    apply(store, s, force=True)
    return current


def clear(store: Store, s: Settings = settings) -> None:
    """Drop every override; the environment takes over again."""
    store.delete_app_settings(KEY)
    apply(store, s, force=True)


def describe(store: Store, s: Settings = settings) -> dict:
    """UI-safe view: no secret ever leaves the server, only whether it is set."""
    saved = stored(store)
    out: dict[str, object] = {}
    for field in FIELDS:
        effective = getattr(s, field)
        source = "ui" if field in saved else ("env" if _ENV_BASELINE[field] else "default")
        if field in SECRET_FIELDS:
            out[field] = {
                "set": bool(effective),
                "hint": _hint(str(effective)),
                "source": source,
            }
        else:
            out[field] = {"value": effective, "source": source}
    out["admin_password_from_env"] = True
    return out


def _hint(secret: str) -> str:
    """Show just enough of a secret to recognise it, never enough to use it."""
    if not secret:
        return ""
    if len(secret) <= 8:
        return "•" * len(secret)
    return f"{secret[:3]}{'•' * 6}{secret[-3:]}"
