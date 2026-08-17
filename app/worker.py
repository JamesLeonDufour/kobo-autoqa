"""Worker loop: drains the job queue and polls Kobo for missed submissions."""
from __future__ import annotations

import logging
import signal
import time
from datetime import datetime, timedelta, timezone

from .common import make_client, make_store, setup_logging, submission_uuid
from .config import settings
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


def main() -> None:
    setup_logging(settings.log_level)
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    client = make_client(settings)
    store = make_store(settings)
    pipeline = Pipeline(settings, client, store)

    log.info(
        "Worker up. server=%s assets=%s transcript=%s translations=%s qual=%s dry_run=%s",
        settings.kobo_url, settings.asset_uids or "(webhook only)",
        settings.transcript_language, settings.translation_languages,
        settings.enable_qual, settings.dry_run,
    )

    last_poll = 0.0
    while _running:
        # 1. Polling catch-up. Assets enabled in the admin UI are merged with
        #    whatever ASSET_UIDS lists, so either configuration path works.
        watched = sorted(set(settings.asset_uids) | set(store.watched_assets()))
        if watched and (time.time() - last_poll) >= settings.poll_interval_seconds:
            for uid in watched:
                try:
                    poll_asset(client, store, uid, settings.poll_lookback_minutes)
                except Exception:  # noqa: BLE001
                    log.exception("Poll failed for %s", uid)
            last_poll = time.time()

        # 2. Drain ready jobs
        jobs = store.claim_ready(limit=25)
        for job in jobs:
            if not _running:
                break
            pipeline.process(
                job["asset_uid"], job["submission_uuid"], job["stage"], job["attempts"]
            )

        if not jobs:
            time.sleep(settings.worker_tick_seconds)

    client.close()
    log.info("Worker stopped. Final job counts: %s", store.stats())


if __name__ == "__main__":
    main()
