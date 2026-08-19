"""Worker loop: drains the job queue and polls Kobo for missed submissions."""
from __future__ import annotations

import logging
import signal
import time
from datetime import datetime, timedelta, timezone

from . import runtime
from .common import make_store, setup_logging, submission_uuid
from .config import settings
from .kobo import KoboClient, KoboError
from .pipeline import Pipeline

log = logging.getLogger(__name__)
_running = True


def _stop(signum, frame):  # noqa: ANN001, ARG001
    global _running
    log.info("Signal %s received, shutting down after current pass", signum)
    _running = False


def poll_asset(client, store, asset_uid: str, lookback_minutes: int) -> int:
    """Enqueue submissions newer than the stored cursor. Returns count added."""
    cursor = store.get_cursor(asset_uid)
    if not cursor:
        cursor = (datetime.now(timezone.utc) - timedelta(minutes=lookback_minutes)) \
            .strftime("%Y-%m-%dT%H:%M:%S")

    query = {"_submission_time": {"$gte": cursor}}
    added = 0
    newest = cursor
    for sub in client.iter_submissions(
        asset_uid,
        query=query,
        sort={"_submission_time": 1},
        fields=["_id", "_uuid", "meta/rootUuid", "_submission_time"],
    ):
        uuid = submission_uuid(sub)
        stamp = sub.get("_submission_time")
        if stamp and stamp > newest:
            newest = stamp
        if uuid and store.enqueue(asset_uid, uuid, {"source": "poll"}):
            added += 1

    if newest != cursor:
        store.set_cursor(asset_uid, newest)
    if added:
        log.info("Poll %s: enqueued %s new submission(s), cursor -> %s", asset_uid, added, newest)
    return added


class Tenants:
    """Per-account Kobo clients, built on demand and reused between passes.

    Each account has its own server and token, so a job can only be handled
    with its owner's credentials. Clients are rebuilt when that account's
    connection changes.
    """

    def __init__(self, store) -> None:
        self._store = store
        self._cache: dict[int, tuple[float, object, Pipeline]] = {}
        self._quiet: set[int] = set()

    def pipeline(self, owner: int) -> Pipeline | None:
        stamp = self._store.app_settings_updated_at(runtime.key_for(owner))
        cached = self._cache.get(owner)
        if cached and cached[0] == stamp:
            return cached[2]
        if cached:
            cached[1].close()

        s = runtime.for_owner(self._store, owner)
        try:
            s.validate()
        except RuntimeError as exc:
            if owner not in self._quiet:
                log.warning("Account %s has no usable credentials yet (%s)", owner, exc)
                self._quiet.add(owner)
            self._cache.pop(owner, None)
            return None
        self._quiet.discard(owner)

        client = KoboClient(s.kobo_url, s.kobo_token, verify=s.verify_tls,
                            timeout=s.http_timeout)
        owned = self._store.for_owner(owner)
        pipe = Pipeline(s, client, owned)
        self._cache[owner] = (stamp, client, pipe)
        log.info("Account %s connected to %s", owner, s.kobo_url)
        return pipe

    def settings_for(self, owner: int) -> object:
        return runtime.for_owner(self._store, owner)

    def close(self) -> None:
        for _stamp, client, _pipe in self._cache.values():
            client.close()
        self._cache.clear()


def main() -> None:
    setup_logging(settings.log_level)
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    store = make_store(settings)
    tenants = Tenants(store)
    last_poll = 0.0

    log.info("Worker up. Serving every active account; idling until any has credentials.")

    while _running:
        # 1. Polling catch-up, per account.
        if (time.time() - last_poll) >= settings.poll_interval_seconds:
            # owner 0 holds anything configured before accounts existed.
            for owner in [0, *store.active_user_ids()]:
                pipe = tenants.pipeline(owner)
                if pipe is None:
                    continue
                s = tenants.settings_for(owner)
                owned = store.for_owner(owner)
                watched = sorted(set(s.asset_uids) | set(owned.watched_assets()))
                for uid in watched:
                    try:
                        poll_asset(pipe.client, owned, uid, s.poll_lookback_minutes)
                    except KoboError as exc:
                        if exc.status == 404:
                            # The form was deleted in KoboToolbox. Nothing here
                            # can bring it back, and re-asking every few minutes
                            # buries real problems under a repeating traceback.
                            log.warning("Form %s no longer exists (account %s); "
                                        "no longer polling it", uid, owner)
                            owned.set_asset_settings(
                                uid, {**owned.get_asset_settings(uid), "enabled": False,
                                      "missing": True})
                            continue
                        log.error("Poll failed for %s (account %s): %s", uid, owner, exc)
                    except Exception:  # noqa: BLE001
                        log.exception("Poll failed for %s (account %s)", uid, owner)
            last_poll = time.time()

        # 2. Drain ready jobs, routing each to its owner's pipeline.
        jobs = store.claim_ready(limit=25)
        for job in jobs:
            if not _running:
                break
            owner = job["owner_id"] if "owner_id" in job.keys() else 0
            pipe = tenants.pipeline(owner)
            if pipe is None:
                # No credentials for that account; try again next pass.
                store.for_owner(owner).advance(
                    job["asset_uid"], job["submission_uuid"], job["stage"], delay=300,
                    note="waiting for this account's Kobo credentials")
                continue
            keys = job.keys()
            pipe.process(job["asset_uid"], job["submission_uuid"],
                         job["stage"], job["attempts"],
                         failures=(job["failures"] if "failures" in keys else 0),
                         created_at=(job["created_at"] if "created_at" in keys else 0.0))

        if not jobs:
            time.sleep(settings.worker_tick_seconds)

    tenants.close()
    log.info("Worker stopped. Final job counts: %s", store.stats())


if __name__ == "__main__":
    main()
