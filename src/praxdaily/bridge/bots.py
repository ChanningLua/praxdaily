"""Bot registration — bind an iLink wxid to one or more personas.

A wxid can serve multiple personas (the demo case where a single
``clawbot`` carries every character). When that's true, outbound
messages get a `【persona.name】` prefix automatically. When a wxid
serves exactly one persona, the prefix is dropped.
"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime


@dataclass
class Bot:
    wxid: str
    persona_id: str
    ilink_account_id: str
    role: str = "primary"
    status: str = "active"
    capacity: int = 3000
    friend_count: int = 0
    created_at: str = ""

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Bot":
        return cls(
            wxid=row["wxid"],
            persona_id=row["persona_id"],
            ilink_account_id=row["ilink_account_id"],
            role=row["role"],
            status=row["status"],
            capacity=row["capacity"],
            friend_count=row["friend_count"],
            created_at=row["created_at"],
        )

    def to_dict(self) -> dict:
        return asdict(self)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def register(
    conn: sqlite3.Connection,
    *,
    wxid: str,
    persona_id: str,
    ilink_account_id: str,
    role: str = "primary",
    capacity: int = 3000,
) -> Bot:
    """Bind a wxid to a persona. Same wxid may be registered for many
    personas; (wxid, persona_id) is the unique key."""
    if not wxid or not persona_id or not ilink_account_id:
        raise ValueError("wxid, persona_id, ilink_account_id all required")
    if role not in {"primary", "backup"}:
        raise ValueError(f"invalid role: {role!r}")
    conn.execute(
        """
        INSERT INTO bots (wxid, persona_id, ilink_account_id, role, status, capacity, created_at)
        VALUES (?, ?, ?, ?, 'active', ?, ?)
        ON CONFLICT(wxid, persona_id) DO UPDATE SET
            ilink_account_id=excluded.ilink_account_id,
            role=excluded.role,
            capacity=excluded.capacity
        """,
        (wxid, persona_id, ilink_account_id, role, capacity, _now()),
    )
    conn.commit()
    return get(conn, wxid=wxid, persona_id=persona_id)


def get(conn: sqlite3.Connection, *, wxid: str, persona_id: str) -> Bot:
    row = conn.execute(
        "SELECT * FROM bots WHERE wxid = ? AND persona_id = ?", (wxid, persona_id)
    ).fetchone()
    if row is None:
        raise KeyError(f"bot {wxid!r} for persona {persona_id!r}")
    return Bot.from_row(row)


def list_for_persona(conn: sqlite3.Connection, persona_id: str) -> list[Bot]:
    rows = conn.execute(
        "SELECT * FROM bots WHERE persona_id = ? ORDER BY role ASC, created_at ASC",
        (persona_id,),
    ).fetchall()
    return [Bot.from_row(r) for r in rows]


def list_active_for_persona(conn: sqlite3.Connection, persona_id: str) -> list[Bot]:
    rows = conn.execute(
        "SELECT * FROM bots WHERE persona_id = ? AND status = 'active' "
        "ORDER BY role ASC, created_at ASC",
        (persona_id,),
    ).fetchall()
    return [Bot.from_row(r) for r in rows]


def personas_for_wxid(conn: sqlite3.Connection, wxid: str) -> list[str]:
    """All persona_ids served by this wxid. Used to decide whether to
    prefix outbound messages and to scope sticky reply routing."""
    rows = conn.execute(
        "SELECT persona_id FROM bots WHERE wxid = ?", (wxid,)
    ).fetchall()
    return [r["persona_id"] for r in rows]


def list_all(conn: sqlite3.Connection) -> list[Bot]:
    rows = conn.execute(
        "SELECT * FROM bots ORDER BY persona_id ASC, role ASC, created_at ASC"
    ).fetchall()
    return [Bot.from_row(r) for r in rows]


def set_status(conn: sqlite3.Connection, *, wxid: str, persona_id: str, status: str) -> None:
    if status not in {"active", "degraded", "banned"}:
        raise ValueError(f"invalid status: {status!r}")
    conn.execute(
        "UPDATE bots SET status = ? WHERE wxid = ? AND persona_id = ?",
        (status, wxid, persona_id),
    )
    conn.commit()


def unregister(conn: sqlite3.Connection, *, wxid: str, persona_id: str) -> None:
    conn.execute(
        "DELETE FROM bots WHERE wxid = ? AND persona_id = ?", (wxid, persona_id)
    )
    conn.commit()
