"""Record an inbound user reply against a persona.

Webhook (or simulated POST) hands us ``(bot_wxid, target_wxid, content)``.
We:

  1. Find all bindings on this bot from this user (one per persona).
  2. Run replyctx.route to pick the persona this message belongs to.
  3. Persist to ``messages`` (direction='in') and update sticky.
  4. Return ``(persona_id, message_row_id, reason)`` for the caller to
     forward to the chat APP via its callback.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any

from . import binding_tokens as binding_tokens_mod
from . import bindings as bindings_mod
from . import replyctx


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def receive(
    conn: sqlite3.Connection,
    *,
    bot_wxid: str,
    target_wxid: str,
    content: str,
) -> dict[str, Any]:
    """Process an inbound message. Returns:

      {
        "matched": bool,
        "persona_id": str | None,
        "app_user_id": str | None,
        "reason": str,            # routing reason (sticky/default/...)
        "msg_id": int | None,
      }

    ``matched=False`` means no binding was found — the message came from
    a stranger (not yet bound to any persona). Caller should ignore or
    surface for diagnostics.

    Special case: if the message contains a pending binding token issued
    for this bot, we treat the message as a binding-claim and complete
    the binding instead of routing it as a normal reply.
    """
    # 1. Token claim takes precedence over normal routing — even if the
    #    user is already bound to other personas, sending a fresh token
    #    means they're claiming a new persona binding.
    token_match = binding_tokens_mod.find_pending_in_content(
        conn, bot_wxid=bot_wxid, content=content
    )
    if token_match is not None:
        try:
            bound, binding_id = binding_tokens_mod.complete(
                conn, token=token_match.token, target_wxid=target_wxid
            )
        except ValueError as exc:
            return {
                "matched": False,
                "persona_id": token_match.persona_id,
                "app_user_id": token_match.app_user_id,
                "reason": f"token_complete_failed: {exc}",
                "msg_id": None,
                "kind": "binding_claim",
            }
        # Don't write this as an inbox message — it's a system event, not chat.
        return {
            "matched": True,
            "persona_id": bound.persona_id,
            "app_user_id": bound.app_user_id,
            "reason": "binding_claimed",
            "msg_id": None,
            "kind": "binding_claim",
            "binding_id": binding_id,
            "token": bound.token,
        }

    bindings = bindings_mod.find_for_inbound(
        conn, bot_wxid=bot_wxid, target_wxid=target_wxid
    )
    if not bindings:
        return {
            "matched": False,
            "persona_id": None,
            "app_user_id": None,
            "reason": "no_binding",
            "msg_id": None,
        }

    # All bindings here share app_user_id (1 user can bind many personas
    # to the same wxid in the demo case).
    app_user_id = bindings[0].app_user_id
    candidate_persona_ids = [b.persona_id for b in bindings]

    persona_id, reason = replyctx.route(
        conn,
        app_user_id=app_user_id,
        bot_wxid=bot_wxid,
        candidate_persona_ids=candidate_persona_ids,
        content=content,
    )
    if persona_id is None:
        return {
            "matched": False,
            "persona_id": None,
            "app_user_id": app_user_id,
            "reason": reason,
            "msg_id": None,
        }

    cursor = conn.execute(
        """
        INSERT INTO messages (app_user_id, persona_id, direction,
                              content, status, created_at)
        VALUES (?, ?, 'in', ?, 'received', ?)
        """,
        (app_user_id, persona_id, content, _now()),
    )
    conn.commit()
    msg_id = cursor.lastrowid

    # Update sticky so subsequent replies stay with this persona.
    replyctx.touch(
        conn,
        app_user_id=app_user_id,
        bot_wxid=bot_wxid,
        persona_id=persona_id,
    )

    return {
        "matched": True,
        "persona_id": persona_id,
        "app_user_id": app_user_id,
        "reason": reason,
        "msg_id": msg_id,
    }


def list_inbox(
    conn: sqlite3.Connection,
    *,
    persona_id: str,
    app_user_id: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Return recent messages (both directions) for a persona, optionally
    filtered to a single user. Newest first."""
    if app_user_id:
        rows = conn.execute(
            "SELECT msg_id, app_user_id, persona_id, direction, content, "
            "status, error, created_at FROM messages "
            "WHERE persona_id = ? AND app_user_id = ? "
            "ORDER BY created_at DESC LIMIT ?",
            (persona_id, app_user_id, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT msg_id, app_user_id, persona_id, direction, content, "
            "status, error, created_at FROM messages "
            "WHERE persona_id = ? ORDER BY created_at DESC LIMIT ?",
            (persona_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]
