"""One-to-many broadcast over bridge bindings.

Used by the daily digest pipeline to fan out the day's chunks to every
active subscriber. Per-user failures are isolated — one user with a
broken binding does not stop the others.

Design choices:

- **No persona / kf concept here.** Broadcast is a generic "send these
  chunks to all active subscribers" operation. The daily digest does
  not have a persona; if a future app wants persona-aware broadcast
  it should layer that on top.

- **Idempotency per (user, chunk_index).** When the cron retries a
  partially-failed broadcast, the second pass skips chunks that were
  already sent successfully on the first pass. Keyed on
  ``f"{idempotency_prefix}:{app_user_id}:{chunk_index}"``.

- **Rate limiting.** Inside a single user's chunks we space sends by
  ``inter_chunk_delay_s`` (default 2s, matches existing pipeline behavior
  to dodge iLink session-context loss). Between users we add
  ``inter_user_delay_s`` (default 0.5s) so a 100-user broadcast doesn't
  hammer iLink in a tight loop.

- **iLink ret=-2 retry.** The same 3-attempt exponential-backoff retry
  used by the legacy ``pipeline._push_chunks`` is reused — moved here
  so all bridge-mediated sends share one place.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import datetime
from typing import Any

from . import bindings as bindings_mod
from . import rate_limit as rate_limit_mod
from .bindings import Binding


logger = logging.getLogger(__name__)


INTER_CHUNK_DELAY_S = 2.0
INTER_USER_DELAY_S = 0.5
RETRY_ATTEMPTS = 3
RETRY_BACKOFF_BASE_S = 2.5


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _idem_key(prefix: str, app_user_id: str, chunk_index: int) -> str | None:
    if not prefix:
        return None
    return f"{prefix}:{app_user_id}:{chunk_index}"


async def _send_one_chunk(
    *,
    binding: Binding,
    body: str,
    apply_rate_limit: bool = True,
) -> tuple[bool, str]:
    """Network-only send via prax.tools.notify. Returns (ok, error_str).
    Includes the same 3-attempt retry the legacy pipeline uses so iLink
    transient ret=-2 doesn't break the broadcast.

    ``apply_rate_limit`` defaults to True; tests pass False to avoid
    interacting with the global token bucket.
    """
    if apply_rate_limit:
        await rate_limit_mod.acquire_outbound(binding.app_user_id)

    try:
        from prax.tools.notify import build_provider  # type: ignore
    except ImportError as exc:
        return False, f"praxagent not importable: {exc}"

    cfg = {
        "provider": "wechat_personal",
        "account_id": binding.ilink_account_id,
        "to": binding.target_wxid,
    }
    try:
        provider = build_provider(cfg)
    except ValueError as exc:
        return False, str(exc)

    last_exc: Exception | None = None
    for attempt in range(RETRY_ATTEMPTS):
        if attempt > 0:
            await asyncio.sleep(RETRY_BACKOFF_BASE_S * attempt)
        try:
            await provider.send(title="", body=body, level="info")
            return True, ""
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
    return False, f"{type(last_exc).__name__}: {last_exc}" if last_exc else "unknown"


def _record_message(
    conn: sqlite3.Connection,
    *,
    binding: Binding,
    content: str,
    status: str,
    error: str = "",
    idempotency_key: str | None = None,
) -> int:
    # Schema currently requires persona_id NOT NULL. Broadcast is
    # persona-less, so we use an empty-string sentinel — the inbox UI
    # treats `persona_id == ''` as "system broadcast" and groups it
    # accordingly. A future schema migration can relax this column.
    cur = conn.execute(
        """
        INSERT INTO messages (idempotency_key, app_user_id, persona_id,
                              direction, content, status, error, created_at)
        VALUES (?, ?, ?, 'out', ?, ?, ?, ?)
        """,
        (idempotency_key, binding.app_user_id, "", content, status, error, _now()),
    )
    conn.commit()
    return cur.lastrowid


async def broadcast_chunks(
    conn: sqlite3.Connection,
    *,
    chunks: list[str],
    inter_chunk_delay_s: float = INTER_CHUNK_DELAY_S,
    inter_user_delay_s: float = INTER_USER_DELAY_S,
    idempotency_prefix: str = "",
    apply_rate_limit: bool = True,
    cwd=None,
    apply_moderation: bool = True,
) -> dict[str, Any]:
    """Send ``chunks`` to every active subscriber.

    Returns a summary dict:

      {
        "users_total":  int,   # active bindings at start
        "users_sent":   int,   # users who received >=1 chunk OK
        "users_failed": int,   # users with at least one failed chunk
        "chunks_sent":  int,   # successful chunk-sends across all users
        "chunks_total": int,   # users_total * len(chunks)
        "failures":     [...]  # per-user error info for diagnostics
      }
    """
    active = bindings_mod.list_active(conn)
    chunks_total = len(active) * len(chunks)
    if not active:
        return {
            "users_total": 0, "users_sent": 0, "users_failed": 0,
            "chunks_sent": 0, "chunks_total": 0, "failures": [],
        }

    # Pre-screen chunks for sensitive words. Block the WHOLE broadcast
    # on any hit — partial sends across users would leave subscribers
    # in inconsistent states, and a single sensitive chunk usually
    # indicates an upstream content issue worth pausing for review.
    if apply_moderation and cwd is not None:
        from . import moderation
        for c_idx, chunk in enumerate(chunks):
            ok, hit = moderation.check(chunk, cwd=cwd)
            if not ok:
                from . import alerts as _alerts
                _alerts.fire_and_forget(
                    kind="content_blocked",
                    severity="error",
                    message=(
                        f"broadcast aborted: chunk {c_idx + 1}/{len(chunks)} "
                        f"contains sensitive word {hit!r}"
                    ),
                    dedup_key=f"content_blocked:{hit}",
                )
                logger.error(
                    "broadcast: blocked chunk %d/%d on sensitive word %r — aborting",
                    c_idx + 1, len(chunks), hit,
                )
                # Record the block to messages table for audit (against
                # one synthetic binding so the dashboard inbox sees it).
                conn.execute(
                    """
                    INSERT INTO messages (idempotency_key, app_user_id, persona_id,
                                          direction, content, status, error, created_at)
                    VALUES (NULL, '_system_', '', 'out', ?, 'blocked', ?, ?)
                    """,
                    (chunk, f"sensitive word: {hit}", _now()),
                )
                conn.commit()
                return {
                    "users_total": len(active),
                    "users_sent": 0,
                    "users_failed": 0,
                    "chunks_sent": 0,
                    "chunks_total": chunks_total,
                    "failures": [],
                    "blocked": True,
                    "blocked_chunk_index": c_idx,
                    "blocked_word": hit,
                }

    users_sent = 0
    users_failed = 0
    chunks_sent = 0
    failures: list[dict[str, Any]] = []

    for u_idx, b in enumerate(active):
        if u_idx > 0 and inter_user_delay_s > 0:
            await asyncio.sleep(inter_user_delay_s)

        user_ok = True
        per_user_chunks_sent = 0

        for c_idx, chunk in enumerate(chunks):
            if c_idx > 0 and inter_chunk_delay_s > 0:
                await asyncio.sleep(inter_chunk_delay_s)

            idem = _idem_key(idempotency_prefix, b.app_user_id, c_idx)

            # Idempotency check — skip already-sent.
            if idem:
                existing = conn.execute(
                    "SELECT status FROM messages WHERE idempotency_key = ?",
                    (idem,),
                ).fetchone()
                if existing is not None and existing["status"] == "sent":
                    logger.info(
                        "broadcast: skip already-sent user=%s chunk=%d (idem=%s)",
                        b.app_user_id, c_idx, idem,
                    )
                    chunks_sent += 1
                    per_user_chunks_sent += 1
                    continue

            ok, err = await _send_one_chunk(
                binding=b, body=chunk, apply_rate_limit=apply_rate_limit,
            )
            if ok:
                _record_message(
                    conn, binding=b, content=chunk, status="sent",
                    idempotency_key=idem,
                )
                chunks_sent += 1
                per_user_chunks_sent += 1
            else:
                _record_message(
                    conn, binding=b, content=chunk, status="failed",
                    error=err, idempotency_key=idem,
                )
                user_ok = False
                logger.warning(
                    "broadcast: failed user=%s chunk=%d/%d: %s",
                    b.app_user_id, c_idx + 1, len(chunks), err,
                )
                from . import alerts as _alerts
                _alerts.fire_and_forget(
                    kind="send_failure",
                    severity="warn",
                    message=(
                        f"broadcast send failed for {b.app_user_id} "
                        f"(bot={b.bot_wxid}): {err}"
                    ),
                    dedup_key=f"send_failure:{b.app_user_id}",
                )
                failures.append({
                    "app_user_id": b.app_user_id,
                    "chunk_index": c_idx,
                    "error": err,
                })
                # Stop trying to send remaining chunks to this user — most
                # likely cause is bot/binding broken; continuing wastes
                # network and risks exacerbating rate-limit issues.
                break

        if user_ok and per_user_chunks_sent == len(chunks):
            users_sent += 1
        else:
            users_failed += 1

    return {
        "users_total": len(active),
        "users_sent": users_sent,
        "users_failed": users_failed,
        "chunks_sent": chunks_sent,
        "chunks_total": chunks_total,
        "failures": failures,
    }
