"""Bridge SQLite schema — 5 tables, single migration runner.

Tables:
  personas       — virtual characters defined by the product
  bots           — wxid ↔ persona binding (a wxid may serve multiple personas)
  bindings       — app_user_id ↔ (persona_id, bot_wxid, target_wxid)
  conversations  — sticky reply-routing state per (app_user_id, bot_wxid)
  messages       — append-only out/in record for inbox + idempotency
"""

from __future__ import annotations

import sqlite3


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS personas (
    persona_id      TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    avatar_url      TEXT NOT NULL DEFAULT '',
    intro           TEXT NOT NULL DEFAULT '',
    card_image_path TEXT NOT NULL DEFAULT '',
    is_default      INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bots (
    wxid              TEXT NOT NULL,
    persona_id        TEXT NOT NULL,
    ilink_account_id  TEXT NOT NULL,
    role              TEXT NOT NULL DEFAULT 'primary',
    status            TEXT NOT NULL DEFAULT 'active',
    capacity          INTEGER NOT NULL DEFAULT 3000,
    friend_count      INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT NOT NULL,
    PRIMARY KEY (wxid, persona_id),
    FOREIGN KEY (persona_id) REFERENCES personas(persona_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS bindings (
    binding_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    app_user_id       TEXT NOT NULL,
    persona_id        TEXT NOT NULL,
    bot_wxid          TEXT NOT NULL,
    ilink_account_id  TEXT NOT NULL,
    target_wxid       TEXT NOT NULL,
    status            TEXT NOT NULL DEFAULT 'bound',
    note              TEXT NOT NULL DEFAULT '',
    created_at        TEXT NOT NULL,
    UNIQUE (app_user_id, persona_id, status)
);

CREATE INDEX IF NOT EXISTS idx_bindings_user
    ON bindings(app_user_id);
CREATE INDEX IF NOT EXISTS idx_bindings_botwx
    ON bindings(bot_wxid, target_wxid);

CREATE TABLE IF NOT EXISTS conversations (
    app_user_id     TEXT NOT NULL,
    bot_wxid        TEXT NOT NULL,
    persona_id      TEXT NOT NULL,
    last_active_at  TEXT NOT NULL,
    PRIMARY KEY (app_user_id, bot_wxid)
);

CREATE TABLE IF NOT EXISTS messages (
    msg_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    idempotency_key  TEXT UNIQUE,
    app_user_id      TEXT NOT NULL,
    persona_id       TEXT NOT NULL,
    direction        TEXT NOT NULL,  -- 'out' | 'in'
    content          TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'sent',
    error            TEXT NOT NULL DEFAULT '',
    created_at       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_persona_user
    ON messages(persona_id, app_user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_messages_user
    ON messages(app_user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS binding_tokens (
    token             TEXT PRIMARY KEY,
    app_user_id       TEXT NOT NULL,
    persona_id        TEXT NOT NULL,
    bot_wxid          TEXT NOT NULL,
    ilink_account_id  TEXT NOT NULL,
    status            TEXT NOT NULL DEFAULT 'pending',  -- pending | bound | expired
    binding_id        INTEGER,
    expires_at        TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    completed_at      TEXT,
    -- iLink-QR-based binding fields (when token is paired with a live
    -- iLink login QR; client polls qrcode_status until 'confirmed' and
    -- the captured ilink_user_id becomes target_wxid for the binding):
    ilink_qrcode_value TEXT NOT NULL DEFAULT '',
    ilink_base_url     TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_binding_tokens_status
    ON binding_tokens(status, expires_at);
"""


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    """Add a column if it's missing. ``CREATE TABLE IF NOT EXISTS`` only
    skips creation when the table exists — it does NOT add new columns
    to an existing table — so for in-place upgrades we need this."""
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    if any(r[1] == column for r in rows):
        return
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)
    # In-place migrations for columns added after first release.
    _ensure_column(conn, "binding_tokens", "ilink_qrcode_value", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "binding_tokens", "ilink_base_url", "TEXT NOT NULL DEFAULT ''")
