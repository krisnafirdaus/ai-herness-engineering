"""Database connection + schema management for SQLite (default) and Postgres.

The repository layer (``models.py``) is written ONCE against a neutral SQL with
``?`` placeholders and ``%(pk)s`` for the auto-increment primary key. A thin
:class:`ConnectionProxy` adapts that single SQL to the active backend:

* SQLite  — placeholders pass through; ``INTEGER PRIMARY KEY AUTOINCREMENT``.
* Postgres — ``?`` is rewritten to ``%s`` and the pk type to ``BIGSERIAL``;
  ``psycopg`` with ``dict_row`` gives the same row-by-name access as sqlite3.Row.

This keeps the data-access code backend-agnostic, so ``HARNESS_DATABASE_URL``
alone selects the store. SQLite is the verified default; Postgres is wired for
the ``infra/`` production topology.
"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from ..config import settings

_local = threading.local()


def _dialect() -> str:
    return "postgres" if settings.database_url.startswith(
        ("postgres://", "postgresql://")) else "sqlite"


class ConnectionProxy:
    """Adapts neutral SQL (``?`` placeholders, ``{PK}`` token) to the backend."""

    def __init__(self, raw, dialect: str) -> None:
        self._raw = raw
        self._dialect = dialect

    def _adapt(self, sql: str) -> str:
        sql = sql.replace("{PK}", "INTEGER PRIMARY KEY AUTOINCREMENT"
                          if self._dialect == "sqlite" else "BIGSERIAL PRIMARY KEY")
        if self._dialect == "postgres":
            sql = sql.replace("?", "%s")
        return sql

    def execute(self, sql: str, params: tuple = ()):  # returns a cursor
        return self._raw.execute(self._adapt(sql), params)

    def executescript(self, script: str) -> None:
        if self._dialect == "sqlite":
            self._raw.executescript(self._adapt(script))
            return
        # psycopg has no executescript: run statements individually.
        for stmt in filter(str.strip, self._adapt(script).split(";")):
            self._raw.execute(stmt)


def _connect_sqlite():
    path = settings.database_url[len("sqlite:///"):]
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)  # autocommit
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _connect_postgres():
    import psycopg
    from psycopg.rows import dict_row

    return psycopg.connect(settings.database_url, autocommit=True, row_factory=dict_row)


def get_connection() -> ConnectionProxy:
    """Return a thread-local connection proxy (one per thread/worker)."""
    conn = getattr(_local, "conn", None)
    if conn is not None:
        return conn
    dialect = _dialect()
    raw = _connect_postgres() if dialect == "postgres" else _connect_sqlite()
    conn = ConnectionProxy(raw, dialect)
    _local.conn = conn
    return conn


# Neutral schema: `{PK}` is expanded per-dialect; placeholders are `?`.
SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id            TEXT PRIMARY KEY,
    repo_url          TEXT NOT NULL,
    task              TEXT NOT NULL,
    branch            TEXT,
    workspace_path    TEXT,
    base_ref          TEXT,
    status            TEXT NOT NULL,
    current_step      INTEGER NOT NULL DEFAULT 0,
    total_steps       INTEGER NOT NULL DEFAULT 0,
    plan_json         TEXT,
    tokens_used       INTEGER NOT NULL DEFAULT 0,
    error             TEXT,
    pr_url            TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS steps (
    id            {PK},
    run_id        TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    step_index    INTEGER NOT NULL,
    step_id       TEXT NOT NULL,
    file          TEXT NOT NULL,
    action        TEXT NOT NULL,
    reason        TEXT,
    checks_json   TEXT NOT NULL,
    depends_on_json TEXT,
    status        TEXT NOT NULL,
    iterations    INTEGER NOT NULL DEFAULT 0,
    last_error    TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    UNIQUE(run_id, step_index)
);

CREATE TABLE IF NOT EXISTS telemetry (
    id                     {PK},
    run_id                 TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    step_id                TEXT,
    agent                  TEXT NOT NULL,
    input_tokens           INTEGER NOT NULL DEFAULT 0,
    output_tokens          INTEGER NOT NULL DEFAULT 0,
    duration_ms            INTEGER NOT NULL DEFAULT 0,
    verification_iteration INTEGER,
    status                 TEXT,
    created_at             TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id          {PK},
    run_id      TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    ts          TEXT NOT NULL,
    level       TEXT NOT NULL,
    stage       TEXT,
    message     TEXT NOT NULL,
    data_json   TEXT
);

CREATE INDEX IF NOT EXISTS idx_runs_status   ON runs(status);
CREATE INDEX IF NOT EXISTS idx_steps_run     ON steps(run_id, step_index);
CREATE INDEX IF NOT EXISTS idx_tel_run       ON telemetry(run_id);
CREATE INDEX IF NOT EXISTS idx_events_run    ON events(run_id, id);
"""


# Columns added after the initial schema shipped. Existing databases are
# upgraded in place by an additive ALTER TABLE (safe under both dialects);
# "duplicate column" failures mean the column already exists and are ignored.
_MIGRATIONS = [
    ("runs", "pr_url", "ALTER TABLE runs ADD COLUMN pr_url TEXT"),
    ("steps", "depends_on_json", "ALTER TABLE steps ADD COLUMN depends_on_json TEXT"),
]


def _apply_migrations(conn: ConnectionProxy) -> None:
    for _table, _column, ddl in _MIGRATIONS:
        try:
            conn.execute(ddl)
        except Exception:
            pass  # column already present


def init_db() -> None:
    """Create all tables/indexes if absent, then apply additive migrations."""
    conn = get_connection()
    conn.executescript(SCHEMA)
    _apply_migrations(conn)
