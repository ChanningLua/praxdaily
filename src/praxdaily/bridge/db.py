"""SQLite connection helper for the bridge module.

The DB lives at ``<cwd>/.prax/bridge.db`` so it's per-workspace, same
discipline as ``.prax/notify.yaml``. ``connect()`` opens a fresh
connection per call (cheap for SQLite) and applies the schema if the
file is missing.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path


def db_path(cwd) -> Path:
    return Path(cwd) / ".prax" / "bridge.db"


def connect(cwd) -> sqlite3.Connection:
    """Open a connection. Schema init runs on every connect — every
    statement is ``CREATE TABLE IF NOT EXISTS``, so this is cheap and
    keeps existing DBs in sync as new tables/indexes are added."""
    path = db_path(cwd)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    from .schema import init_schema
    init_schema(conn)
    conn.commit()
    return conn
