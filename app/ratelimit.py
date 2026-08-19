"""Per-IP rate limiting for the endpoints anyone can reach without a session.

`/login` and `/register` are the only doors into this app from the open
internet, and both are cheap to hammer. scrypt makes each password guess cost
about 50ms, which is a real deterrent but not a limit -- nothing stopped a
caller from simply making a great many of them, or from filling the users
table with pending accounts.

The window is in-process and deliberately so: only the `api` container serves
HTTP, and a limiter that forgets everything on restart is the right trade for
one with no dependencies. It is a brake on automated abuse, not an audit log.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request

log = logging.getLogger(__name__)

_hits: dict[str, deque[float]] = defaultdict(deque)
_lock = threading.Lock()

# Enough headroom that a person mistyping a password never notices, low enough
# that guessing at scale is pointless.
LOGIN = (10, 300)        # 10 attempts per 5 minutes
REGISTER = (5, 3600)     # 5 new accounts per hour


def client_ip(request: Request) -> str:
    """The caller's address, as seen through the reverse proxy.

    Nginx Proxy Manager sets X-Forwarded-For; its first entry is the original
    client. This is only ever used as a rate-limit key -- never for access
    control -- so a spoofed value costs its sender their own bucket and
    nothing else.
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def check(request: Request, bucket: str, limit: tuple[int, int]) -> None:
    """Raise 429 when this caller has used up `limit` for the window."""
    count, window = limit
    key = f"{bucket}:{client_ip(request)}"
    now = time.time()
    with _lock:
        seen = _hits[key]
        while seen and seen[0] <= now - window:
            seen.popleft()
        if len(seen) >= count:
            retry = int(seen[0] + window - now) + 1
            log.warning("Rate limited %s on %s (%s attempts in %ss)",
                        client_ip(request), bucket, len(seen), window)
            raise HTTPException(
                status_code=429,
                detail=f"Too many attempts. Try again in {retry} seconds.",
                headers={"Retry-After": str(retry)},
            )
        seen.append(now)
        # Keep the table from growing without bound on a long-lived process.
        if len(_hits) > 10_000:
            for stale in [k for k, v in _hits.items() if not v or v[-1] <= now - 3600]:
                del _hits[stale]


def forget(request: Request, bucket: str) -> None:
    """Drop a caller's record after they succeed, so a legitimate user who
    mistyped their password a few times is not still throttled afterwards."""
    with _lock:
        _hits.pop(f"{bucket}:{client_ip(request)}", None)
