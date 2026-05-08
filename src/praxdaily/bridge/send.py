"""Outbound send via iLink, with auto-prefix logic.

Given a (persona, binding) pair, build a one-shot ``wechat_personal``
provider config and call ``provider.send()``. If the bot wxid serves
multiple personas, prefix the body with `【persona.name】`; otherwise
send it bare.

Inter-message rate-limit and the ``ret=-2`` retry policy mirror what
``pipeline._push_chunks`` already does — keeping behaviour identical so
operators see one consistent failure mode.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import datetime
from typing import Any

from . import bots as bots_mod
from .bindings import Binding
from .personas import Persona


logger = logging.getLogger(__name__)

INTER_MSG_DELAY_S = 2.0
RETRY_ATTEMPTS = 3


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def render_outbound(persona: Persona, content: str, *, prefix: bool) -> str:
    """Return the body string actually sent to WeChat.

    When ``prefix`` is True (multi-persona on one wxid), wrap with
    `【persona.name】content`. When False (one persona per wxid),
    return content untouched — the wxid identity already conveys the
    speaker.
    """
    if not prefix:
        return content
    return f"【{persona.name}】{content}"


async def send_via_binding(
    conn: sqlite3.Connection,
    *,
    persona: Persona,
    binding: Binding,
    content: str,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Send ``content`` from ``persona`` to the user described by
    ``binding``. Returns a dict with ``sent: bool`` and either
    ``msg_id`` or ``error``.

    Persists the outbound message to ``messages`` regardless of outcome
    (so the inbox/audit log is complete) and updates ``conversations``
    sticky on success.
    """
    # Idempotency: if a row with this key already exists, skip the network call.
    if idempotency_key:
        existing = conn.execute(
            "SELECT msg_id, status FROM messages WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
        if existing is not None:
            return {
                "sent": existing["status"] == "sent",
                "msg_id": existing["msg_id"],
                "deduped": True,
            }

    persona_count = len(bots_mod.personas_for_wxid(conn, binding.bot_wxid))
    body = render_outbound(persona, content, prefix=persona_count > 1)

    try:
        from prax.tools.notify import build_provider  # type: ignore
    except ImportError as exc:
        return _record_failure(
            conn, persona=persona, binding=binding, content=content,
            idempotency_key=idempotency_key,
            error=f"praxagent not importable: {exc}",
        )

    cfg = {
        "provider": "wechat_personal",
        "account_id": binding.ilink_account_id,
        "to": binding.target_wxid,
    }
    try:
        provider = build_provider(cfg)
    except ValueError as exc:
        return _record_failure(
            conn, persona=persona, binding=binding, content=content,
            idempotency_key=idempotency_key, error=str(exc),
        )

    last_exc: Exception | None = None
    for attempt in range(RETRY_ATTEMPTS):
        if attempt > 0:
            await asyncio.sleep(INTER_MSG_DELAY_S * attempt)
        try:
            await provider.send(title="", body=body, level="info")
            last_exc = None
            break
        except Exception as exc:  # noqa: BLE001
            last_exc = exc

    if last_exc is not None:
        return _record_failure(
            conn, persona=persona, binding=binding, content=content,
            idempotency_key=idempotency_key,
            error=f"{type(last_exc).__name__}: {last_exc}",
        )

    msg_id = _record_success(
        conn, persona=persona, binding=binding, content=content,
        idempotency_key=idempotency_key,
    )

    # Touch sticky so future user replies route here.
    from . import replyctx
    replyctx.touch(
        conn,
        app_user_id=binding.app_user_id,
        bot_wxid=binding.bot_wxid,
        persona_id=persona.persona_id,
    )

    return {"sent": True, "msg_id": msg_id, "rendered": body}


def _record_success(
    conn: sqlite3.Connection,
    *,
    persona: Persona,
    binding: Binding,
    content: str,
    idempotency_key: str | None,
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO messages (idempotency_key, app_user_id, persona_id, direction,
                              content, status, created_at)
        VALUES (?, ?, ?, 'out', ?, 'sent', ?)
        """,
        (idempotency_key, binding.app_user_id, persona.persona_id, content, _now()),
    )
    conn.commit()
    return cursor.lastrowid


def _record_failure(
    conn: sqlite3.Connection,
    *,
    persona: Persona,
    binding: Binding,
    content: str,
    idempotency_key: str | None,
    error: str,
) -> dict[str, Any]:
    cursor = conn.execute(
        """
        INSERT INTO messages (idempotency_key, app_user_id, persona_id, direction,
                              content, status, error, created_at)
        VALUES (?, ?, ?, 'out', ?, 'failed', ?, ?)
        """,
        (idempotency_key, binding.app_user_id, persona.persona_id, content,
         error, _now()),
    )
    conn.commit()
    return {"sent": False, "msg_id": cursor.lastrowid, "error": error}
