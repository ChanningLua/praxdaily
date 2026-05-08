"""Bindings — link a chat-APP user to a (persona, bot, target_wxid) triple.

A binding answers the runtime question: "When the APP says 'send msg
from persona P to user U', which iLink account do I dial and which
target wxid is the recipient?"

For demo we expose ``manual_bind`` — the operator types in the user's
wxid in the dashboard. Production replaces this with QR-scan auto-bind
(``binding/qrcode`` flow) once iLink ``friend_request`` payload semantics
are confirmed.
"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime

from . import bots as bots_mod


@dataclass
class Binding:
    binding_id: int
    app_user_id: str
    persona_id: str
    bot_wxid: str
    ilink_account_id: str
    target_wxid: str
    status: str
    note: str
    created_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Binding":
        return cls(
            binding_id=row["binding_id"],
            app_user_id=row["app_user_id"],
            persona_id=row["persona_id"],
            bot_wxid=row["bot_wxid"],
            ilink_account_id=row["ilink_account_id"],
            target_wxid=row["target_wxid"],
            status=row["status"],
            note=row["note"],
            created_at=row["created_at"],
        )

    def to_dict(self) -> dict:
        return asdict(self)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def manual_bind(
    conn: sqlite3.Connection,
    *,
    app_user_id: str,
    persona_id: str,
    target_wxid: str,
    bot_wxid: str | None = None,
    note: str = "",
) -> Binding:
    """Demo-time binding. Operator picks app_user_id + persona + target wxid;
    we resolve which bot in that persona's pool will serve them.

    If ``bot_wxid`` is omitted, the first active bot in the persona's pool
    is selected. Only one ``bound`` binding per (app_user_id, persona_id) is
    allowed; older ones are flipped to ``superseded``.
    """
    if not app_user_id or not persona_id or not target_wxid:
        raise ValueError("app_user_id, persona_id, target_wxid required")

    pool = bots_mod.list_active_for_persona(conn, persona_id)
    if not pool:
        raise ValueError(f"persona {persona_id!r} has no active bot — register one first")

    if bot_wxid is None:
        chosen = pool[0]
    else:
        match = [b for b in pool if b.wxid == bot_wxid]
        if not match:
            raise ValueError(
                f"bot {bot_wxid!r} not registered (or not active) for persona {persona_id!r}"
            )
        chosen = match[0]

    # Supersede any prior bound row for this (user, persona).
    conn.execute(
        "UPDATE bindings SET status = 'superseded' "
        "WHERE app_user_id = ? AND persona_id = ? AND status = 'bound'",
        (app_user_id, persona_id),
    )
    conn.execute(
        """
        INSERT INTO bindings (app_user_id, persona_id, bot_wxid, ilink_account_id,
                              target_wxid, status, note, created_at)
        VALUES (?, ?, ?, ?, ?, 'bound', ?, ?)
        """,
        (app_user_id, persona_id, chosen.wxid, chosen.ilink_account_id,
         target_wxid, note, _now()),
    )
    conn.commit()
    return get_active(conn, app_user_id=app_user_id, persona_id=persona_id)


def get_active(
    conn: sqlite3.Connection, *, app_user_id: str, persona_id: str
) -> Binding:
    row = conn.execute(
        "SELECT * FROM bindings WHERE app_user_id = ? AND persona_id = ? "
        "AND status = 'bound' ORDER BY binding_id DESC LIMIT 1",
        (app_user_id, persona_id),
    ).fetchone()
    if row is None:
        raise KeyError(f"no active binding for ({app_user_id}, {persona_id})")
    return Binding.from_row(row)


def find_for_inbound(
    conn: sqlite3.Connection, *, bot_wxid: str, target_wxid: str
) -> list[Binding]:
    """Reverse lookup: a message arrived at ``bot_wxid`` from user wxid
    ``target_wxid``. Return all matching bindings (one per persona this
    user is bound to on this bot).
    """
    rows = conn.execute(
        "SELECT * FROM bindings WHERE bot_wxid = ? AND target_wxid = ? "
        "AND status = 'bound' ORDER BY binding_id ASC",
        (bot_wxid, target_wxid),
    ).fetchall()
    return [Binding.from_row(r) for r in rows]


def list_active(conn: sqlite3.Connection) -> list[Binding]:
    """All currently bound bindings, regardless of persona/user.

    Used by the daily broadcast: every active subscription gets the day's
    digest regardless of which persona was originally bound."""
    rows = conn.execute(
        "SELECT * FROM bindings WHERE status = 'bound' ORDER BY created_at ASC"
    ).fetchall()
    return [Binding.from_row(r) for r in rows]


def list_for_user(conn: sqlite3.Connection, app_user_id: str) -> list[Binding]:
    rows = conn.execute(
        "SELECT * FROM bindings WHERE app_user_id = ? AND status = 'bound' "
        "ORDER BY persona_id ASC",
        (app_user_id,),
    ).fetchall()
    return [Binding.from_row(r) for r in rows]


def list_for_persona(conn: sqlite3.Connection, persona_id: str) -> list[Binding]:
    rows = conn.execute(
        "SELECT * FROM bindings WHERE persona_id = ? AND status = 'bound' "
        "ORDER BY created_at DESC",
        (persona_id,),
    ).fetchall()
    return [Binding.from_row(r) for r in rows]


def expire(conn: sqlite3.Connection, binding_id: int) -> None:
    conn.execute(
        "UPDATE bindings SET status = 'expired' WHERE binding_id = ?",
        (binding_id,),
    )
    conn.commit()
