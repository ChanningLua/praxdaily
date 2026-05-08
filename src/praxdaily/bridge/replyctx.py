"""Reply routing — figure out which persona a user's message goes to.

When a wxid serves exactly one persona, the answer is trivial: that
persona. When a wxid serves multiple personas (the demo case where a
shared ``clawbot`` carries every character), we pick by:

1. Explicit switch command (`/切林夕`, `/list`) — handled by caller
2. Sticky last-active within ``STICKY_WINDOW_MIN`` minutes
3. Default persona (``personas.is_default = 1``) as final fallback
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

from . import personas as personas_mod


STICKY_WINDOW_MIN = 30


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def touch(
    conn: sqlite3.Connection,
    *,
    app_user_id: str,
    bot_wxid: str,
    persona_id: str,
) -> None:
    """Mark this (user, bot) ↔ persona pair as recently active."""
    conn.execute(
        """
        INSERT INTO conversations (app_user_id, bot_wxid, persona_id, last_active_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(app_user_id, bot_wxid) DO UPDATE SET
            persona_id = excluded.persona_id,
            last_active_at = excluded.last_active_at
        """,
        (app_user_id, bot_wxid, persona_id, _now()),
    )
    conn.commit()


def get_sticky(
    conn: sqlite3.Connection,
    *,
    app_user_id: str,
    bot_wxid: str,
) -> str | None:
    """Return persona_id if there's an entry within STICKY_WINDOW_MIN."""
    row = conn.execute(
        "SELECT persona_id, last_active_at FROM conversations "
        "WHERE app_user_id = ? AND bot_wxid = ?",
        (app_user_id, bot_wxid),
    ).fetchone()
    if row is None:
        return None
    try:
        last = datetime.fromisoformat(row["last_active_at"])
    except ValueError:
        return None
    if datetime.now() - last > timedelta(minutes=STICKY_WINDOW_MIN):
        return None
    return row["persona_id"]


def parse_switch_command(content: str) -> str | None:
    """Recognize `/切<persona_name>` — returns the requested persona name,
    or None if the message isn't a switch command. Caller resolves name
    → persona_id."""
    if not content:
        return None
    s = content.strip()
    for prefix in ("/切", "/切换", "/switch", "/sw "):
        if s.startswith(prefix):
            rest = s[len(prefix):].strip()
            if rest:
                return rest
    return None


def route(
    conn: sqlite3.Connection,
    *,
    app_user_id: str,
    bot_wxid: str,
    candidate_persona_ids: list[str],
    content: str,
) -> tuple[str | None, str]:
    """Decide the persona for an inbound message.

    Returns ``(persona_id, reason)``. ``persona_id`` is None only if the
    bot is registered for zero personas (mis-config).

    Reasons (for observability):
      - ``single_persona`` — only one persona registered on this bot
      - ``switch_command`` — `/切XXX` matched a candidate
      - ``sticky`` — within window
      - ``default`` — fell back to is_default persona
      - ``arbitrary`` — picked first candidate as last resort
    """
    if not candidate_persona_ids:
        return None, "no_personas"

    if len(candidate_persona_ids) == 1:
        return candidate_persona_ids[0], "single_persona"

    # 1. switch command
    requested_name = parse_switch_command(content)
    if requested_name:
        # Resolve name → id within candidates
        for pid in candidate_persona_ids:
            try:
                p = personas_mod.get(conn, pid)
            except KeyError:
                continue
            if p.name == requested_name or p.persona_id == requested_name:
                return pid, "switch_command"

    # 2. sticky
    sticky = get_sticky(conn, app_user_id=app_user_id, bot_wxid=bot_wxid)
    if sticky and sticky in candidate_persona_ids:
        return sticky, "sticky"

    # 3. default persona
    default = personas_mod.get_default(conn)
    if default and default.persona_id in candidate_persona_ids:
        return default.persona_id, "default"

    # 4. arbitrary first
    return candidate_persona_ids[0], "arbitrary"
