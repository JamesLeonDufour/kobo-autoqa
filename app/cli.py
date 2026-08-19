"""Operations CLI.

    python -m app.cli introspect  <asset_uid>          # what does this server accept?
    python -m app.cli questions   <asset_uid>          # transcribable questions + current features
    python -m app.cli apply-qual  <asset_uid> qual_survey.json
    python -m app.cli register-hook <asset_uid>        # create the Kobo REST Service
    python -m app.cli list-hooks  <asset_uid>
    python -m app.cli backfill    <asset_uid> [--since 2026-08-01] [--limit 500]
    python -m app.cli run-once    <asset_uid> <submission_uuid>
    python -m app.cli supplement  <asset_uid> <submission_uuid>
    python -m app.cli status
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import uuid as uuidlib
from datetime import datetime, timedelta, timezone

from .common import make_client, make_store, setup_logging, submission_uuid
from .config import settings
from . import payloads as P
from .pipeline import Pipeline
from .store import STAGE_NEW


def _dump(obj) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False, default=str))


def cmd_introspect(args) -> int:
    with make_client(settings) as c:
        schema = c.get_advanced_submission_schema(args.asset_uid)
        advanced = c.get_advanced_features(args.asset_uid)
    print("=== advanced_features (asset config) ===")
    _dump(advanced)
    print("\n=== advanced_submission_schema (accepted payload) ===")
    _dump(schema)
    print(f"\nDetected payload dialect: {P.detect_dialect(schema)!r}")
    print("Set SCHEMA_DIALECT in .env to pin it if auto-detection is wrong.")
    return 0


def cmd_questions(args) -> int:
    with make_client(settings) as c:
        asset = c.get_asset(args.asset_uid)
    advanced = asset.get("advanced_features") or {}
    media = P.media_question_xpaths(asset)
    print(f"Form: {asset.get('name')}")
    print(f"Transcribable questions in form : {media or '(none)'}")
    print(f"Questions with advanced features: {P.configured_xpaths(advanced, [])or '(none)'}")
    survey = P.qual_survey(advanced)
    print(f"Preset qual questions           : {len(survey)}")
    for q in survey:
        label = (q.get("labels") or {}).get("_default") or q.get("label") or ""
        print(f"  - [{q.get('type')}] {label}  ({q.get('uuid')})")
    return 0


def cmd_apply_qual(args) -> int:
    with open(args.qual_file, encoding="utf-8") as fh:
        survey = json.load(fh)
    if isinstance(survey, dict):
        survey = survey.get("qual_survey", [])

    # Stable uuids so re-running does not duplicate questions.
    for q in survey:
        if not q.get("uuid"):
            q["uuid"] = str(uuidlib.uuid4())
        q.setdefault("scope", "by_question#survey")
        for choice in q.get("choices") or []:
            if not choice.get("uuid"):
                choice["uuid"] = str(uuidlib.uuid4())

    with make_client(settings) as c:
        asset = c.get_asset(args.asset_uid)
        advanced = asset.get("advanced_features") or {}
        xpaths = args.xpaths or P.media_question_xpaths(asset)
        if not xpaths:
            print("No transcribable questions found; pass --xpaths explicitly.", file=sys.stderr)
            return 1

        advanced.setdefault("transcript", {})["values"] = xpaths
        if settings.translation_languages:
            advanced.setdefault("translation", {})["values"] = xpaths
            advanced["translation"]["languages"] = settings.translation_languages
        advanced.setdefault("qual", {})["values"] = xpaths
        advanced["qual"]["qual_survey"] = survey

        for q in survey:
            q.setdefault("xpath", xpaths[0])

        if args.dry_run:
            print("Would PATCH advanced_features:")
            _dump(advanced)
            return 0
        result = c.set_advanced_features(args.asset_uid, advanced)
    print("Applied. New advanced_features:")
    _dump(result.get("advanced_features"))
    print(f"\nWrite the generated uuids back to {args.qual_file} so re-running is idempotent:")
    _dump({"qual_survey": survey})
    return 0


def cmd_register_hook(args) -> int:
    url = args.url or settings.public_webhook_url
    if not url:
        print("Set PUBLIC_WEBHOOK_URL or pass --url", file=sys.stderr)
        return 1
    endpoint = f"{url.rstrip('/')}/kobo/hook/{args.asset_uid}"
    headers = (
        {settings.webhook_secret_header: settings.webhook_secret}
        if settings.webhook_secret else {}
    )
    with make_client(settings) as c:
        existing = [h for h in c.list_hooks(args.asset_uid) if h.get("endpoint") == endpoint]
        if existing:
            print(f"Hook already exists: {existing[0].get('uid')} -> {endpoint}")
            return 0
        hook = c.create_hook(
            args.asset_uid,
            name=args.name,
            endpoint=endpoint,
            custom_headers=headers,
            subset_fields=[],
        )
    print(f"Created hook {hook.get('uid')} -> {endpoint}")
    return 0


def cmd_list_hooks(args) -> int:
    with make_client(settings) as c:
        _dump(c.list_hooks(args.asset_uid))
    return 0


def cmd_backfill(args) -> int:
    since = args.since or (datetime.now(timezone.utc) - timedelta(days=args.days)) \
        .strftime("%Y-%m-%dT%H:%M:%S")
    store = make_store(settings)
    added = 0
    with make_client(settings) as c:
        for sub in c.iter_submissions(
            args.asset_uid,
            query={"_submission_time": {"$gte": since}},
            sort={"_submission_time": 1},
            fields=["_id", "_uuid", "meta/rootUuid", "_submission_time"],
        ):
            u = submission_uuid(sub)
            if u and store.enqueue(args.asset_uid, u, {"source": "backfill"}):
                added += 1
            if args.limit and added >= args.limit:
                break
    print(f"Enqueued {added} submission(s) since {since}. Start the worker to process them.")
    return 0


def _job_row(store, asset_uid: str, submission_uuid_: str):
    for r in store.list_jobs(limit=1000):
        if r["asset_uid"] == asset_uid and r["submission_uuid"] == submission_uuid_:
            return r
    return None


def cmd_run_once(args) -> int:
    """Drive one submission through every stage in the foreground, verbosely."""
    store = make_store(settings)
    store.enqueue(args.asset_uid, args.submission_uuid, {"source": "cli"})
    store.reset(args.asset_uid, args.submission_uuid)

    with make_client(settings) as c:
        pipeline = Pipeline(settings, c, store)
        for i in range(args.passes):
            row = _job_row(store, args.asset_uid, args.submission_uuid)
            stage = row["stage"] if row else STAGE_NEW
            if stage in ("done", "failed"):
                break
            print(f"-- pass {i + 1}/{args.passes}: stage={stage}")
            pipeline.process(args.asset_uid, args.submission_uuid, stage, 0)
            after = _job_row(store, args.asset_uid, args.submission_uuid)
            if after and after["stage"] in ("done", "failed"):
                break
            # Clear the worker-collision backoff so the next pass runs now.
            store.advance(args.asset_uid, args.submission_uuid,
                          after["stage"] if after else STAGE_NEW, delay=0)
            time.sleep(args.wait)

        final = _job_row(store, args.asset_uid, args.submission_uuid)
        print("\n=== final job state ===")
        _dump(dict(final) if final else None)
        print("\n=== supplementalDetails now in Kobo ===")
        _dump(c.get_supplement(args.asset_uid, args.submission_uuid))
    return 0


def cmd_supplement(args) -> int:
    with make_client(settings) as c:
        _dump(c.get_supplement(args.asset_uid, args.submission_uuid))
    return 0


def cmd_status(args) -> int:  # noqa: ARG001
    store = make_store(settings)
    print("Job counts:", store.stats())
    for r in store.list_jobs(limit=args.limit):
        print(f"  {r['asset_uid']}/{r['submission_uuid']}  stage={r['stage']:<10} "
              f"attempts={r['attempts']:<3} err={(r['last_error'] or '')[:80]}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="app.cli", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("introspect"); s.add_argument("asset_uid"); s.set_defaults(fn=cmd_introspect)
    s = sub.add_parser("questions"); s.add_argument("asset_uid"); s.set_defaults(fn=cmd_questions)

    s = sub.add_parser("apply-qual")
    s.add_argument("asset_uid"); s.add_argument("qual_file")
    s.add_argument("--xpaths", nargs="*", default=None)
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(fn=cmd_apply_qual)

    s = sub.add_parser("register-hook")
    s.add_argument("asset_uid")
    s.add_argument("--url", default=None)
    s.add_argument("--name", default="AutoQA pipeline")
    s.set_defaults(fn=cmd_register_hook)

    s = sub.add_parser("list-hooks"); s.add_argument("asset_uid"); s.set_defaults(fn=cmd_list_hooks)

    s = sub.add_parser("backfill")
    s.add_argument("asset_uid")
    s.add_argument("--since", default=None, help="ISO timestamp, e.g. 2026-08-01T00:00:00")
    s.add_argument("--days", type=int, default=7)
    s.add_argument("--limit", type=int, default=0)
    s.set_defaults(fn=cmd_backfill)

    s = sub.add_parser("run-once")
    s.add_argument("asset_uid"); s.add_argument("submission_uuid")
    s.add_argument("--passes", type=int, default=10)
    s.add_argument("--wait", type=int, default=20)
    s.set_defaults(fn=cmd_run_once)

    s = sub.add_parser("supplement")
    s.add_argument("asset_uid"); s.add_argument("submission_uuid")
    s.set_defaults(fn=cmd_supplement)

    s = sub.add_parser("status")
    s.add_argument("--limit", type=int, default=25)
    s.set_defaults(fn=cmd_status)

    return p


def main() -> int:
    setup_logging(settings.log_level)
    args = build_parser().parse_args()
    try:
        return args.fn(args)
    except RuntimeError as exc:
        # Settings.validate() phrases itself for the admin UI, because that is
        # where nearly everyone configures this. Someone running the CLI may
        # have no such screen at all -- CLI-only mode is by definition a
        # deployment with the UI switched off -- so name the environment
        # variables here rather than pointing at a page that does not exist.
        print(f"error: {exc}", file=sys.stderr)
        print("Set KOBO_URL and KOBO_TOKEN in the environment or .env.",
              file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
