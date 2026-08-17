"""Shared helpers: logging setup, submission uuid extraction, wiring."""
from __future__ import annotations

import logging
import sys

from .config import Settings, settings
from .kobo import KoboClient
from .store import Store


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def make_client(s: Settings = settings) -> KoboClient:
    s.validate()
    return KoboClient(s.kobo_url, s.kobo_token, verify=s.verify_tls, timeout=s.http_timeout)


def make_store(s: Settings = settings) -> Store:
    return Store(s.db_path)


def submission_uuid(submission: dict) -> str | None:
    """The uuid the subsequences API keys on: meta/rootUuid, else _uuid.

    Both come through with a `uuid:` prefix in some kpi versions; strip it.
    """
    raw = (
        (submission.get("meta") or {}).get("rootUuid")
        or submission.get("meta/rootUuid")
        or submission.get("_uuid")
        or submission.get("formhub/uuid")
    )
    if not raw:
        return None
    return str(raw).removeprefix("uuid:")


def asset_uid_from_submission(submission: dict) -> str | None:
    """Kobo REST Service payloads carry the form uid in a few possible fields."""
    for key in ("_xform_id_string", "__version__", "asset_uid"):
        val = submission.get(key)
        if isinstance(val, str) and val.startswith("a") and len(val) > 15:
            return val
    val = submission.get("_xform_id_string")
    return val if isinstance(val, str) else None
