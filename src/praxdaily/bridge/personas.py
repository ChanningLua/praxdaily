"""Persona CRUD + dataclass.

Each persona is a virtual character the chat APP exposes. ``persona_id``
is the stable opaque key that the APP backend should reference. Other
fields are display-only (rendered in chat as `【name】` prefix when the
persona shares a wxid with siblings).
"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime


@dataclass
class Persona:
    persona_id: str
    name: str
    avatar_url: str = ""
    intro: str = ""
    card_image_path: str = ""
    is_default: bool = False
    created_at: str = ""
    updated_at: str = ""

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Persona":
        return cls(
            persona_id=row["persona_id"],
            name=row["name"],
            avatar_url=row["avatar_url"],
            intro=row["intro"],
            card_image_path=row["card_image_path"],
            is_default=bool(row["is_default"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def to_dict(self) -> dict:
        d = asdict(self)
        d["is_default"] = bool(d["is_default"])
        return d


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def upsert(
    conn: sqlite3.Connection,
    *,
    persona_id: str,
    name: str,
    avatar_url: str = "",
    intro: str = "",
    card_image_path: str = "",
    is_default: bool = False,
) -> Persona:
    """Insert or update a persona by persona_id.

    If ``is_default`` is set, the previous default is cleared so exactly
    zero or one persona is the default at any time.
    """
    if not persona_id or not name:
        raise ValueError("persona_id and name are required")
    now = _now()
    if is_default:
        conn.execute("UPDATE personas SET is_default = 0, updated_at = ?", (now,))
    conn.execute(
        """
        INSERT INTO personas (persona_id, name, avatar_url, intro, card_image_path,
                              is_default, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(persona_id) DO UPDATE SET
            name=excluded.name,
            avatar_url=excluded.avatar_url,
            intro=excluded.intro,
            card_image_path=excluded.card_image_path,
            is_default=excluded.is_default,
            updated_at=excluded.updated_at
        """,
        (persona_id, name, avatar_url, intro, card_image_path,
         1 if is_default else 0, now, now),
    )
    conn.commit()
    return get(conn, persona_id)


def get(conn: sqlite3.Connection, persona_id: str) -> Persona:
    row = conn.execute(
        "SELECT * FROM personas WHERE persona_id = ?", (persona_id,)
    ).fetchone()
    if row is None:
        raise KeyError(persona_id)
    return Persona.from_row(row)


def list_all(conn: sqlite3.Connection) -> list[Persona]:
    rows = conn.execute(
        "SELECT * FROM personas ORDER BY is_default DESC, created_at ASC"
    ).fetchall()
    return [Persona.from_row(r) for r in rows]


def delete(conn: sqlite3.Connection, persona_id: str) -> None:
    """Delete persona + cascade clears bots row via FK; bindings/conversations
    are left in place but will fail to send (status reflects this)."""
    conn.execute("DELETE FROM personas WHERE persona_id = ?", (persona_id,))
    conn.commit()


def get_default(conn: sqlite3.Connection) -> Persona | None:
    row = conn.execute(
        "SELECT * FROM personas WHERE is_default = 1 LIMIT 1"
    ).fetchone()
    return Persona.from_row(row) if row else None
