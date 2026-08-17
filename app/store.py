"""SQLite-backed job store.

One row per (asset_uid, submission_uuid). The row carries a stage so the
worker can advance a submission through transcription -> translation -> qual
across many short passes instead of blocking on Kobo's async NLP jobs.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

STAGE_NEW = "new"
STAGE_TRANSCRIBE = "transcribe"
STAGE_TRANSLATE = "translate"
STAGE_QUAL = "qual"
STAGE_DONE = "done"
STAGE_FAILED = "failed"

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    asset_uid       TEXT NOT NULL,
    submission_uuid TEXT NOT NULL,
    stage           TEXT NOT NULL DEFAULT 'new',
    attempts        INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL DEFAULT 0,
    last_error      TEXT,
    payload         TEXT,
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL,
    PRIMARY KEY (asset_uid, submission_uuid)
);
CREATE INDEX IF NOT EXISTS idx_jobs_ready ON jobs (stage, next_attempt_at);

CREATE TABLE IF NOT EXISTS cursors (
    asset_uid TEXT PRIMARY KEY,
    last_seen TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS asset_settings (
    asset_uid  TEXT PRIMARY KEY,
    config     TEXT NOT NULL,
    updated_at REAL NOT NULL
);
"""


class Store:
    def __init__(self, db_path: str) -> None:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db_path = db_path
        self._lock = threading.Lock()
        with self._conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self._db_path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            yield conn
            conn.commit()
        finally:
            conn.close()

    # -- enqueue ------------------------------------------------------------
    def enqueue(self, asset_uid: str, submission_uuid: str, payload: dict | None = None) -> bool:
        """Insert a job if absent. Returns True when newly created."""
        now = time.time()
        with self._lock, self._conn() as c:
            cur = c.execute(
                """
                INSERT OR IGNORE INTO jobs
                    (asset_uid, submission_uuid, stage, attempts, next_attempt_at,
                     payload, created_at, updated_at)
                VALUES (?, ?, ?, 0, ?, ?, ?, ?)
                """,
                (asset_uid, submission_uuid, STAGE_NEW, now,
                 json.dumps(payload or {}), now, now),
            )
            return cur.rowcount > 0

    # -- claim / update -----------------------------------------------------
    def claim_ready(self, limit: int = 20) -> list[sqlite3.Row]:
        now = time.time()
        with self._lock, self._conn() as c:
            rows = c.execute(
                """
                SELECT * FROM jobs
                WHERE stage NOT IN (?, ?) AND next_attempt_at <= ?
                ORDER BY next_attempt_at ASC
                LIMIT ?
                """,
                (STAGE_DONE, STAGE_FAILED, now, limit),
            ).fetchall()
            # Push out next_attempt_at so a concurrent worker does not double-take.
            for row in rows:
                c.execute(
                    "UPDATE jobs SET next_attempt_at = ? WHERE asset_uid = ? AND submission_uuid = ?",
                    (now + 120, row["asset_uid"], row["submission_uuid"]),
                )
            return rows

    def advance(self, asset_uid: str, submission_uuid: str, stage: str,
                delay: float = 0.0, error: str | None = None,
                bump_attempts: bool = False) -> None:
        now = time.time()
        with self._lock, self._conn() as c:
            c.execute(
                f"""
                UPDATE jobs
                SET stage = ?,
                    next_attempt_at = ?,
                    last_error = ?,
                    updated_at = ?,
                    attempts = attempts + {1 if bump_attempts else 0}
                WHERE asset_uid = ? AND submission_uuid = ?
                """,
                (stage, now + delay, error, now, asset_uid, submission_uuid),
            )

    def stats(self) -> dict[str, int]:
        with self._conn() as c:
            rows = c.execute("SELECT stage, COUNT(*) n FROM jobs GROUP BY stage").fetchall()
        return {r["stage"]: r["n"] for r in rows}

    def list_jobs(self, stage: str | None = None, limit: int = 50) -> list[sqlite3.Row]:
        q = "SELECT * FROM jobs"
        args: tuple = ()
        if stage:
            q += " WHERE stage = ?"
            args = (stage,)
        q += " ORDER BY updated_at DESC LIMIT ?"
        with self._conn() as c:
            return c.execute(q, args + (limit,)).fetchall()

    def reset(self, asset_uid: str, submission_uuid: str) -> None:
        self.advance(asset_uid, submission_uuid, STAGE_NEW, delay=0, error=None)

    # -- per-asset settings (UI-managed overrides) --------------------------
    def get_asset_settings(self, asset_uid: str) -> dict:
        with self._conn() as c:
            row = c.execute(
                "SELECT config FROM asset_settings WHERE asset_uid = ?", (asset_uid,)
            ).fetchone()
        return json.loads(row["config"]) if row else {}

    def all_asset_settings(self) -> dict[str, dict]:
        with self._conn() as c:
            rows = c.execute("SELECT asset_uid, config FROM asset_settings").fetchall()
        return {r["asset_uid"]: json.loads(r["config"]) for r in rows}

    def set_asset_settings(self, asset_uid: str, config: dict) -> None:
        with self._lock, self._conn() as c:
            c.execute(
                "INSERT INTO asset_settings (asset_uid, config, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(asset_uid) DO UPDATE SET config = excluded.config, "
                "updated_at = excluded.updated_at",
                (asset_uid, json.dumps(config), time.time()),
            )

    def delete_asset_settings(self, asset_uid: str) -> None:
        with self._lock, self._conn() as c:
            c.execute("DELETE FROM asset_settings WHERE asset_uid = ?", (asset_uid,))

    def watched_assets(self) -> list[str]:
        """Assets the UI has enabled, regardless of ASSET_UIDS in .env."""
        return sorted(
            uid for uid, cfg in self.all_asset_settings().items()
            if cfg.get("enabled", True)
        )

    # -- poll cursor --------------------------------------------------------
    def get_cursor(self, asset_uid: str) -> str | None:
        with self._conn() as c:
            row = c.execute("SELECT last_seen FROM cursors WHERE asset_uid = ?", (asset_uid,)).fetchone()
        return row["last_seen"] if row else None

    def set_cursor(self, asset_uid: str, last_seen: str) -> None:
        with self._lock, self._conn() as c:
            c.execute(
                "INSERT INTO cursors (asset_uid, last_seen) VALUES (?, ?) "
                "ON CONFLICT(asset_uid) DO UPDATE SET last_seen = excluded.last_seen",
                (asset_uid, last_seen),
            )
