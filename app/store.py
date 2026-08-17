"""SQLite-backed job store.

One row per (asset_uid, submission_uuid). The row carries a stage so the
worker can advance a submission through transcription -> translation -> qual
across many short passes instead of blocking on Kobo's async NLP jobs.
"""
from __future__ import annotations

import json
import logging
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

log = logging.getLogger(__name__)

STAGE_NEW = "new"
STAGE_TRANSCRIBE = "transcribe"
STAGE_TRANSLATE = "translate"
STAGE_QUAL = "qual"
STAGE_DONE = "done"
STAGE_FAILED = "failed"

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    owner_id        INTEGER NOT NULL DEFAULT 0,
    asset_uid       TEXT NOT NULL,
    submission_uuid TEXT NOT NULL,
    stage           TEXT NOT NULL DEFAULT 'new',
    attempts        INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL DEFAULT 0,
    last_error      TEXT,
    note            TEXT,
    payload         TEXT,
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL,
    PRIMARY KEY (owner_id, asset_uid, submission_uuid)
);
CREATE INDEX IF NOT EXISTS idx_jobs_ready ON jobs (stage, next_attempt_at);

CREATE TABLE IF NOT EXISTS cursors (
    owner_id  INTEGER NOT NULL DEFAULT 0,
    asset_uid TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    PRIMARY KEY (owner_id, asset_uid)
);

CREATE TABLE IF NOT EXISTS asset_settings (
    owner_id   INTEGER NOT NULL DEFAULT 0,
    asset_uid  TEXT NOT NULL,
    config     TEXT NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (owner_id, asset_uid)
);

CREATE TABLE IF NOT EXISTS app_settings (
    key        TEXT PRIMARY KEY,
    config     TEXT NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    email         TEXT NOT NULL UNIQUE COLLATE NOCASE,
    name          TEXT NOT NULL DEFAULT '',
    password_hash TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'pending',
    is_admin      INTEGER NOT NULL DEFAULT 0,
    created_at    REAL NOT NULL,
    approved_at   REAL,
    last_login_at REAL
);
"""

# Everything a user configures is scoped to them. owner_id 0 is the pre-accounts
# data, which the first admin inherits on upgrade.
OWNED_TABLES = ("jobs", "cursors", "asset_settings")


class Store:
    """SQLite access, optionally bound to one user.

    A store with `owner` set reads and writes only that user's rows, so the
    callers (admin API, pipeline) need no per-query scoping and cannot forget
    it. An unbound store spans every user and is what the worker uses to drain
    the queue.
    """

    def __init__(self, db_path: str, owner: int | None = None) -> None:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db_path = db_path
        self._owner = owner
        self._lock = threading.Lock()
        with self._conn() as c:
            self._migrate(c)
            c.executescript(SCHEMA)
            c.execute("CREATE INDEX IF NOT EXISTS idx_jobs_owner ON jobs (owner_id, stage)")

    @staticmethod
    def _migrate(c) -> None:
        """Bring a pre-accounts database up to the owner-scoped schema.

        The owner has to be part of each primary key -- two users may watch the
        same form on different servers -- and SQLite cannot alter a key in
        place, so the tables are rebuilt. Existing rows become owner 0, which
        the first admin account claims on sign-up; nothing is lost.
        """
        existing = {r["name"] for r in
                    c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table in OWNED_TABLES:
            if table not in existing:
                continue
            cols = [r["name"] for r in c.execute(f"PRAGMA table_info({table})")]
            if "owner_id" in cols:
                continue
            if table == "jobs" and "note" not in cols:
                c.execute("ALTER TABLE jobs ADD COLUMN note TEXT")
                cols.append("note")
            shared = ", ".join(cols)
            c.execute(f"ALTER TABLE {table} RENAME TO {table}_old")
            c.executescript(SCHEMA)
            c.execute(f"INSERT INTO {table} (owner_id, {shared}) "
                      f"SELECT 0, {shared} FROM {table}_old")
            c.execute(f"DROP TABLE {table}_old")
            log.info("Migrated %s to owner-scoped schema", table)

    def for_owner(self, owner: int) -> "Store":
        """A view of the same database limited to one user's rows."""
        view = object.__new__(Store)
        view._db_path = self._db_path
        view._owner = owner
        view._lock = self._lock
        return view

    @property
    def owner(self) -> int | None:
        return self._owner

    def _scope(self, prefix: str = "AND") -> tuple[str, tuple]:
        """SQL fragment restricting a query to the bound user, if any."""
        if self._owner is None:
            return "", ()
        return f" {prefix} owner_id = ?", (self._owner,)

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
                     payload, created_at, updated_at, owner_id)
                VALUES (?, ?, ?, 0, ?, ?, ?, ?, ?)
                """,
                (asset_uid, submission_uuid, STAGE_NEW, now,
                 json.dumps(payload or {}), now, now, self._owner or 0),
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
            # Scoped by owner as well: two users may watch the same form.
            for row in rows:
                c.execute(
                    "UPDATE jobs SET next_attempt_at = ? "
                    "WHERE asset_uid = ? AND submission_uuid = ? AND owner_id = ?",
                    (now + 120, row["asset_uid"], row["submission_uuid"], row["owner_id"]),
                )
            return rows

    def advance(self, asset_uid: str, submission_uuid: str, stage: str,
                delay: float = 0.0, error: str | None = None,
                bump_attempts: bool = False, note: str | None = None) -> None:
        """`note` says what this pass did and what it is waiting for, so the
        attempt count is readable rather than just a number.

        Passing note=None leaves the existing one alone -- callers that only
        reschedule a job (clearing a backoff, requeueing) should not erase the
        explanation the last real pass wrote.
        """
        now = time.time()
        note_sql = "?" if note is not None else "note"
        bump = 1 if bump_attempts else 0
        scope, scope_args = self._scope()
        with self._lock, self._conn() as c:
            c.execute(
                f"""
                UPDATE jobs
                SET stage = ?,
                    next_attempt_at = ?,
                    last_error = ?,
                    note = {note_sql},
                    updated_at = ?,
                    attempts = attempts + {bump}
                WHERE asset_uid = ? AND submission_uuid = ?{scope}
                """,
                (stage, now + delay, error, *([note] if note is not None else []),
                 now, asset_uid, submission_uuid, *scope_args),
            )

    def stats(self) -> dict[str, int]:
        scope, args = self._scope("WHERE")
        with self._conn() as c:
            rows = c.execute(
                f"SELECT stage, COUNT(*) n FROM jobs{scope} GROUP BY stage", args
            ).fetchall()
        return {r["stage"]: r["n"] for r in rows}

    def list_jobs(self, stage: str | None = None, limit: int = 50) -> list[sqlite3.Row]:
        q = "SELECT * FROM jobs WHERE 1=1"
        args: tuple = ()
        if stage:
            q += " AND stage = ?"
            args = (stage,)
        scope, scope_args = self._scope()
        q += scope + " ORDER BY updated_at DESC LIMIT ?"
        with self._conn() as c:
            return c.execute(q, args + scope_args + (limit,)).fetchall()

    def delete_job(self, asset_uid: str, submission_uuid: str) -> int:
        with self._lock, self._conn() as c:
            scope, scope_args = self._scope()
            cur = c.execute(
                "DELETE FROM jobs WHERE asset_uid = ? AND submission_uuid = ?" + scope,
                (asset_uid, submission_uuid, *scope_args),
            )
            return cur.rowcount

    def reset(self, asset_uid: str, submission_uuid: str) -> None:
        self.advance(asset_uid, submission_uuid, STAGE_NEW, delay=0, error=None)

    # -- per-asset settings (UI-managed overrides) --------------------------
    def get_asset_settings(self, asset_uid: str) -> dict:
        with self._conn() as c:
            row = c.execute(
                "SELECT config FROM asset_settings WHERE asset_uid = ?" + self._scope()[0],
                (asset_uid, *self._scope()[1])
            ).fetchone()
        return json.loads(row["config"]) if row else {}

    def all_asset_settings(self) -> dict[str, dict]:
        with self._conn() as c:
            scope, args = self._scope("WHERE")
            rows = c.execute("SELECT asset_uid, config FROM asset_settings" + scope, args).fetchall()
        return {r["asset_uid"]: json.loads(r["config"]) for r in rows}

    def set_asset_settings(self, asset_uid: str, config: dict) -> None:
        with self._lock, self._conn() as c:
            c.execute(
                "INSERT INTO asset_settings (asset_uid, config, updated_at, owner_id) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(owner_id, asset_uid) DO UPDATE SET config = excluded.config, "
                "updated_at = excluded.updated_at",
                (asset_uid, json.dumps(config), time.time(), self._owner or 0),
            )

    def delete_asset_settings(self, asset_uid: str) -> None:
        with self._lock, self._conn() as c:
            c.execute("DELETE FROM asset_settings WHERE asset_uid = ?" + self._scope()[0],
                      (asset_uid, *self._scope()[1]))

    def watched_assets(self) -> list[str]:
        """Assets the UI has enabled, regardless of ASSET_UIDS in .env."""
        return sorted(
            uid for uid, cfg in self.all_asset_settings().items()
            if cfg.get("enabled", True)
        )

    # -- global app settings (UI-managed connection credentials) ------------
    def get_app_settings(self, key: str) -> dict:
        with self._conn() as c:
            row = c.execute(
                "SELECT config FROM app_settings WHERE key = ?", (key,)
            ).fetchone()
        return json.loads(row["config"]) if row else {}

    def app_settings_updated_at(self, key: str) -> float:
        with self._conn() as c:
            row = c.execute(
                "SELECT updated_at FROM app_settings WHERE key = ?", (key,)
            ).fetchone()
        return row["updated_at"] if row else 0.0

    def set_app_settings(self, key: str, config: dict) -> None:
        with self._lock, self._conn() as c:
            c.execute(
                "INSERT INTO app_settings (key, config, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET config = excluded.config, "
                "updated_at = excluded.updated_at",
                (key, json.dumps(config), time.time()),
            )

    def delete_app_settings(self, key: str) -> None:
        with self._lock, self._conn() as c:
            c.execute("DELETE FROM app_settings WHERE key = ?", (key,))

    # -- users --------------------------------------------------------------
    def session_secret(self) -> str:
        """Server-side key for signing session cookies, created on first use."""
        current = self.get_app_settings("session")
        if current.get("secret"):
            return current["secret"]
        secret = secrets.token_urlsafe(32)
        self.set_app_settings("session", {"secret": secret})
        return secret

    def count_users(self) -> int:
        with self._conn() as c:
            return c.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]

    def create_user(self, *, email: str, name: str, password_hash: str,
                    status: str, is_admin: bool) -> int:
        now = time.time()
        with self._lock, self._conn() as c:
            cur = c.execute(
                "INSERT INTO users (email, name, password_hash, status, is_admin,"
                " created_at, approved_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (email, name, password_hash, status, int(is_admin), now,
                 now if status == "active" else None),
            )
            return int(cur.lastrowid)

    def get_user_by_email(self, email: str) -> dict | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        return dict(row) if row else None

    def get_user_by_id(self, user_id: int) -> dict | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return dict(row) if row else None

    def list_users(self) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM users ORDER BY "
                "CASE status WHEN 'pending' THEN 0 ELSE 1 END, created_at"
            ).fetchall()
        return [dict(r) for r in rows]

    def active_user_ids(self) -> list[int]:
        with self._conn() as c:
            rows = c.execute("SELECT id FROM users WHERE status = 'active'").fetchall()
        return [r["id"] for r in rows]

    def set_user_status(self, user_id: int, status: str) -> None:
        with self._lock, self._conn() as c:
            c.execute(
                "UPDATE users SET status = ?, approved_at = COALESCE(approved_at, ?) "
                "WHERE id = ?",
                (status, time.time() if status == "active" else None, user_id),
            )

    def set_user_admin(self, user_id: int, is_admin: bool) -> None:
        with self._lock, self._conn() as c:
            c.execute("UPDATE users SET is_admin = ? WHERE id = ?",
                      (int(is_admin), user_id))

    def set_user_password(self, user_id: int, password_hash: str) -> None:
        with self._lock, self._conn() as c:
            c.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                      (password_hash, user_id))

    def touch_user_login(self, user_id: int) -> None:
        with self._lock, self._conn() as c:
            c.execute("UPDATE users SET last_login_at = ? WHERE id = ?",
                      (time.time(), user_id))

    def delete_user(self, user_id: int) -> None:
        """Remove the account and everything it owns."""
        with self._lock, self._conn() as c:
            for table in OWNED_TABLES:
                c.execute(f"DELETE FROM {table} WHERE owner_id = ?", (user_id,))
            c.execute("DELETE FROM app_settings WHERE key = ?", (f"connection:{user_id}",))
            c.execute("DELETE FROM users WHERE id = ?", (user_id,))

    def claim_unowned(self, user_id: int) -> int:
        """Hand pre-accounts data (owner 0) to the first admin."""
        moved = 0
        with self._lock, self._conn() as c:
            for table in OWNED_TABLES:
                cur = c.execute(f"UPDATE {table} SET owner_id = ? WHERE owner_id = 0",
                                (user_id,))
                moved += cur.rowcount
            legacy = c.execute("SELECT config FROM app_settings WHERE key = 'connection'").fetchone()
            if legacy:
                c.execute(
                    "INSERT INTO app_settings (key, config, updated_at) VALUES (?, ?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET config = excluded.config",
                    (f"connection:{user_id}", legacy["config"], time.time()),
                )
        if moved:
            log.info("Claimed %s pre-existing rows for user %s", moved, user_id)
        return moved

    # -- poll cursor --------------------------------------------------------
    def get_cursor(self, asset_uid: str) -> str | None:
        with self._conn() as c:
            row = c.execute("SELECT last_seen FROM cursors WHERE asset_uid = ?" + self._scope()[0],
                            (asset_uid, *self._scope()[1])).fetchone()
        return row["last_seen"] if row else None

    def set_cursor(self, asset_uid: str, last_seen: str) -> None:
        with self._lock, self._conn() as c:
            c.execute(
                "INSERT INTO cursors (asset_uid, last_seen, owner_id) VALUES (?, ?, ?) "
                "ON CONFLICT(owner_id, asset_uid) DO UPDATE SET last_seen = excluded.last_seen",
                (asset_uid, last_seen, self._owner or 0),
            )
