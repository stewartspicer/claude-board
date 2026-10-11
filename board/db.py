"""SQLite storage: schema and connection setup."""

import os
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    token_hash  TEXT NOT NULL UNIQUE,
    created_at  TEXT NOT NULL,
    active      INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS projects (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    aliases     TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS plans (
    id          INTEGER PRIMARY KEY,
    project_id  INTEGER NOT NULL REFERENCES projects(id),
    name        TEXT NOT NULL,
    doc         TEXT,
    branch      TEXT,
    created_by  INTEGER NOT NULL REFERENCES users(id),
    created_at  TEXT NOT NULL,
    UNIQUE (project_id, name)
);
CREATE TABLE IF NOT EXISTS tasks (
    id          INTEGER PRIMARY KEY,
    project_id  INTEGER NOT NULL REFERENCES projects(id),
    title       TEXT NOT NULL,
    ref         TEXT,
    link        TEXT,
    status      TEXT NOT NULL CHECK (status IN ('open', 'claimed', 'done', 'dropped')),
    claimed_by  INTEGER REFERENCES users(id),
    claimed_at  TEXT,
    created_by  INTEGER NOT NULL REFERENCES users(id),
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    plan_id     INTEGER REFERENCES plans(id),
    commit_sha  TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY,
    project_id  INTEGER REFERENCES projects(id),
    task_id     INTEGER REFERENCES tasks(id),
    author_id   INTEGER NOT NULL REFERENCES users(id),
    kind        TEXT NOT NULL,
    body        TEXT NOT NULL DEFAULT '',
    to_user_id  INTEGER REFERENCES users(id),
    reply_to    INTEGER REFERENCES events(id),
    needs_ack   INTEGER NOT NULL DEFAULT 0,
    acked_at    TEXT,
    ack_reply   TEXT,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_project ON events(project_id, id);
CREATE INDEX IF NOT EXISTS events_to_user ON events(to_user_id, id);
CREATE TABLE IF NOT EXISTS cursors (
    user_id       INTEGER NOT NULL REFERENCES users(id),
    session_id    TEXT NOT NULL,
    last_event_id INTEGER NOT NULL,
    updated_at    TEXT NOT NULL,
    PRIMARY KEY (user_id, session_id)
);
"""


# Columns added after the first release: (table, column, declaration). Applied to existing databases.
MIGRATIONS = [
    ("tasks", "plan_id", "INTEGER REFERENCES plans(id)"),
    ("tasks", "commit_sha", "TEXT"),
]


def default_path() -> Path:
    return Path(os.environ.get("DATA_DIR", "data")) / "board.sqlite3"


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    """Open the database (creating the schema if needed). ``":memory:"`` is allowed for tests."""
    path = str(path or default_path())
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if path != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    for table, column, declaration in MIGRATIONS:
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
