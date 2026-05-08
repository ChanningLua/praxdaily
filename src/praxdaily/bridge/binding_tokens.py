"""Binding tokens — short codes a user sends back via WeChat to claim a
binding without operator typing.

Flow:
  1. APP (or operator) calls ``create()`` with (app_user_id, persona_id).
     A bot is picked from the persona's pool; we mint a 6-char alnum
     token and persist it as ``pending``.
  2. UI renders the token as a QR + a one-line instruction telling the
     user to send the token text to the bot in WeChat.
  3. User sends a message containing the token to the bot.
  4. The webhook handler calls ``find_pending_in_content()`` first; on a
     match it calls ``complete()`` which atomically (a) creates the
     binding row and (b) flips the token to ``bound``.

Tokens use uppercase letters + digits, **excluding** ambiguous characters
``0/O/1/I`` so users typing the code from a paper QR won't confuse pairs.
"""

from __future__ import annotations

import secrets
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any

from . import bots as bots_mod


TOKEN_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
TOKEN_LENGTH = 6
DEFAULT_TTL_MINUTES = 10


@dataclass
class BindingToken:
    token: str
    app_user_id: str
    persona_id: str
    bot_wxid: str
    ilink_account_id: str
    status: str
    binding_id: int | None
    expires_at: str
    created_at: str
    completed_at: str | None
    ilink_qrcode_value: str = ""
    ilink_base_url: str = ""

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "BindingToken":
        keys = row.keys() if hasattr(row, "keys") else []
        return cls(
            token=row["token"],
            app_user_id=row["app_user_id"],
            persona_id=row["persona_id"],
            bot_wxid=row["bot_wxid"],
            ilink_account_id=row["ilink_account_id"],
            status=row["status"],
            binding_id=row["binding_id"],
            expires_at=row["expires_at"],
            created_at=row["created_at"],
            completed_at=row["completed_at"],
            ilink_qrcode_value=row["ilink_qrcode_value"] if "ilink_qrcode_value" in keys else "",
            ilink_base_url=row["ilink_base_url"] if "ilink_base_url" in keys else "",
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _now() -> datetime:
    return datetime.now()


def _now_iso() -> str:
    return _now().isoformat(timespec="seconds")


def _mint() -> str:
    return "".join(secrets.choice(TOKEN_ALPHABET) for _ in range(TOKEN_LENGTH))


def create(
    conn: sqlite3.Connection,
    *,
    app_user_id: str,
    persona_id: str,
    bot_wxid: str | None = None,
    ttl_minutes: int = DEFAULT_TTL_MINUTES,
) -> BindingToken:
    """Mint a new pending token. Picks a bot from the persona's active
    pool; if ``bot_wxid`` is given it must already be registered to the
    persona.

    Any prior pending token for this (app_user_id, persona_id) is
    expired so the user only has one valid code at a time.
    """
    if not app_user_id or not persona_id:
        raise ValueError("app_user_id and persona_id required")

    pool = bots_mod.list_active_for_persona(conn, persona_id)
    if not pool:
        raise ValueError(
            f"persona {persona_id!r} has no active bot — register one first"
        )
    if bot_wxid is None:
        chosen = pool[0]
    else:
        match = [b for b in pool if b.wxid == bot_wxid]
        if not match:
            raise ValueError(
                f"bot {bot_wxid!r} not registered (or not active) for persona {persona_id!r}"
            )
        chosen = match[0]

    # Expire prior pending tokens for the same (user, persona) so only
    # the latest QR is honored.
    conn.execute(
        "UPDATE binding_tokens SET status = 'expired' "
        "WHERE app_user_id = ? AND persona_id = ? AND status = 'pending'",
        (app_user_id, persona_id),
    )

    # Mint a unique token (collisions are astronomical with 32^6 = 1B,
    # but loop on the off chance).
    for _ in range(8):
        token = _mint()
        existing = conn.execute(
            "SELECT 1 FROM binding_tokens WHERE token = ?", (token,)
        ).fetchone()
        if existing is None:
            break
    else:
        raise RuntimeError("failed to mint a unique token")

    expires = (_now() + timedelta(minutes=ttl_minutes)).isoformat(timespec="seconds")
    conn.execute(
        """
        INSERT INTO binding_tokens (token, app_user_id, persona_id, bot_wxid,
                                    ilink_account_id, status, expires_at, created_at)
        VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)
        """,
        (token, app_user_id, persona_id, chosen.wxid, chosen.ilink_account_id,
         expires, _now_iso()),
    )
    conn.commit()
    return get(conn, token)


def get(conn: sqlite3.Connection, token: str) -> BindingToken:
    row = conn.execute(
        "SELECT * FROM binding_tokens WHERE token = ?", (token,)
    ).fetchone()
    if row is None:
        raise KeyError(token)
    return BindingToken.from_row(row)


def get_or_none(conn: sqlite3.Connection, token: str) -> BindingToken | None:
    try:
        return get(conn, token)
    except KeyError:
        return None


def find_pending_in_content(
    conn: sqlite3.Connection, *, bot_wxid: str, content: str
) -> BindingToken | None:
    """Search ``content`` for any active pending token bound to ``bot_wxid``.

    Tokens are uppercase, but we match case-insensitively against the
    user's text — they may type lowercase, mix punctuation, etc. We also
    filter out expired rows on the fly so a stale code never matches.
    """
    if not content:
        return None

    rows = conn.execute(
        "SELECT * FROM binding_tokens WHERE bot_wxid = ? AND status = 'pending'",
        (bot_wxid,),
    ).fetchall()
    if not rows:
        return None

    upper = content.upper()
    now_iso = _now_iso()
    matched: BindingToken | None = None
    for r in rows:
        if r["expires_at"] < now_iso:
            # Lazy expiration — sweep on first opportunity.
            conn.execute(
                "UPDATE binding_tokens SET status = 'expired' WHERE token = ?",
                (r["token"],),
            )
            continue
        if r["token"] in upper:
            if matched is not None:
                # Two pending tokens both present in one message — refuse
                # to guess; user must retry with a single code.
                return None
            matched = BindingToken.from_row(r)
    conn.commit()
    return matched


def complete(
    conn: sqlite3.Connection,
    *,
    token: str,
    target_wxid: str,
) -> tuple[BindingToken, int]:
    """Bind on token match. Returns (token_row, binding_id). Caller
    should already have validated ``find_pending_in_content`` returned
    this token, but we re-check status defensively to avoid double-binding
    if two messages with the same token race.
    """
    from . import bindings as bindings_mod

    row = conn.execute(
        "SELECT * FROM binding_tokens WHERE token = ? AND status = 'pending'",
        (token,),
    ).fetchone()
    if row is None:
        raise ValueError(f"token {token!r} not pending")
    bt = BindingToken.from_row(row)

    binding = bindings_mod.manual_bind(
        conn,
        app_user_id=bt.app_user_id,
        persona_id=bt.persona_id,
        target_wxid=target_wxid,
        bot_wxid=bt.bot_wxid,
        note=f"via token {token}",
    )

    conn.execute(
        "UPDATE binding_tokens SET status = 'bound', binding_id = ?, completed_at = ? "
        "WHERE token = ?",
        (binding.binding_id, _now_iso(), token),
    )
    conn.commit()
    return get(conn, token), binding.binding_id


def attach_ilink_qrcode(
    conn: sqlite3.Connection,
    *,
    token: str,
    qrcode_value: str,
    base_url: str,
) -> None:
    """Record the iLink login QR code paired with a token. Used by the
    iLink-QR-based binding flow: client renders ``qrcode_value`` (or the
    server-supplied scannable URL) and polls until iLink reports
    ``confirmed``, at which point ``finalize_with_ilink_user`` runs."""
    conn.execute(
        "UPDATE binding_tokens SET ilink_qrcode_value = ?, ilink_base_url = ? "
        "WHERE token = ?",
        (qrcode_value, base_url, token),
    )
    conn.commit()


def finalize_with_ilink_user(
    conn: sqlite3.Connection,
    *,
    token: str,
    user_wxid: str,
) -> tuple[BindingToken, int]:
    """Complete a binding using the iLink user_id captured when the user
    scanned the QR. ``user_wxid`` is iLink's ``ilink_user_id`` (the
    scanner's WeChat address)."""
    return complete(conn, token=token, target_wxid=user_wxid)


def list_pending_for_user(
    conn: sqlite3.Connection, *, app_user_id: str
) -> list[BindingToken]:
    rows = conn.execute(
        "SELECT * FROM binding_tokens WHERE app_user_id = ? AND status = 'pending' "
        "ORDER BY created_at DESC",
        (app_user_id,),
    ).fetchall()
    return [BindingToken.from_row(r) for r in rows]
