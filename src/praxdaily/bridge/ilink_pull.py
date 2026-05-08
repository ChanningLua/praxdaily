"""iLink getupdates long-poll loop — turn the push-only client into a
bidirectional one.

praxagent's ``wechat_ilink.client`` deliberately drops hermes-agent's
``getupdates`` long-poll loop ("not needed for push-only"). This module
adds it back: one long-running asyncio task per logged-in iLink account,
each calling ``ilink/bot/getupdates`` in a loop, feeding any received
messages into ``bridge.inbound.receive`` so they trigger the same
routing logic as simulated webhook payloads.

Discovered API shape (probed live, 2026-05-07):

  POST /ilink/bot/getupdates
       body: {"limit": N, "sync_buf": "<cursor>", "base_info": {...}}
            with standard ilink_bot_token Authorization headers
       resp: {"msgs": [...], "sync_buf": "<next_cursor>",
              "get_updates_buf": "<echo_back_cursor>"}

  - server holds the connection ~30s (long-poll); returns immediately on msg
  - empty ``msgs`` is normal idle response
  - ``sync_buf`` advances when there are new messages; pass it back to fetch
    only newer ones on the next call

Message item structure mirrors send_text's payload:

  msg = {
    "from_user_id": "<sender wxid>",
    "to_user_id":   "<bot wxid>",
    "item_list":    [{"type": 1, "text_item": {"text": "..."}}],
    ...
  }
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from . import connect as bridge_connect
from . import inbound as inbound_mod


logger = logging.getLogger(__name__)


EP_GET_UPDATES = "ilink/bot/getupdates"

# Network timeouts. Server-side long-poll caps around 30s; we give 45s
# of room before client-side timeout to avoid spurious retries when the
# connection is actually fine.
LONG_POLL_TIMEOUT_S = 45
RECONNECT_INITIAL_BACKOFF_S = 1.0
RECONNECT_MAX_BACKOFF_S = 60.0
DEFAULT_LIMIT = 20

# iLink message item types (verified from send path).
ITEM_TEXT = 1


@dataclass
class PullerStats:
    started_at: float = 0.0
    polls_total: int = 0
    msgs_total: int = 0
    msgs_routed: int = 0
    msgs_unmatched: int = 0
    last_poll_at: float = 0.0
    last_msg_at: float = 0.0
    last_error: str = ""
    last_error_at: float = 0.0


def _extract_text(msg: dict[str, Any]) -> str:
    """Concatenate all text_item entries in a msg's item_list."""
    parts: list[str] = []
    for it in msg.get("item_list") or []:
        if it.get("type") == ITEM_TEXT:
            ti = it.get("text_item") or {}
            text = ti.get("text") or ""
            if text:
                parts.append(text)
    return "".join(parts)


class ILinkPuller:
    """One puller per iLink account.

    Lifecycle:

        p = ILinkPuller(cwd, account)
        await p.start()       # spawns the background asyncio.Task
        ...                   # task runs until stopped
        await p.stop()        # graceful shutdown, joins the task

    Concurrent safety: methods are intended to be called from the loop
    that owns the FastAPI app. Don't share a puller across loops.
    """

    def __init__(self, cwd, account, *, bridge_module=None, inbound_callback=None):
        self.cwd = Path(str(cwd))
        self.account = account
        self.stats = PullerStats()
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._sync_buf: str = ""
        # Allow injecting a different bridge module in tests. Defaults to
        # the real one.
        self._bridge = bridge_module
        # Optional async callback fired after a message is matched to a
        # binding. Signature: ``async fn(conn, app_user_id, content) ->
        # None``. App-layer code (e.g. ``apps.daily_qa.handle``) wires
        # itself in here. Failures in the callback are logged but do not
        # abort the puller loop.
        self._inbound_callback = inbound_callback

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if self.is_running:
            return
        self._stop.clear()
        loop = asyncio.get_running_loop()
        self.stats.started_at = loop.time()
        self._task = asyncio.create_task(self._run(), name=f"ilink-pull[{self.account.account_id}]")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is None:
            return
        try:
            await asyncio.wait_for(self._task, timeout=5.0)
        except asyncio.TimeoutError:
            self._task.cancel()
            with contextlib_suppress():
                await self._task
        finally:
            self._task = None

    async def _run(self) -> None:
        """Outer loop: poll forever with exponential backoff on errors."""
        backoff = RECONNECT_INITIAL_BACKOFF_S
        async with httpx.AsyncClient(
            trust_env=True,
            timeout=httpx.Timeout(LONG_POLL_TIMEOUT_S, connect=5.0),
        ) as client:
            while not self._stop.is_set():
                try:
                    await self._poll_once(client)
                    backoff = RECONNECT_INITIAL_BACKOFF_S  # reset on success
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    self._record_error(str(exc))
                    logger.warning(
                        "ilink_pull[%s]: poll error (will retry in %.1fs): %s",
                        self.account.account_id, backoff, exc,
                    )
                    # Only alert when we hit max backoff (sustained failure),
                    # not on each transient blip — saves the channel from
                    # noise on a flaky network.
                    if backoff >= RECONNECT_MAX_BACKOFF_S:
                        from . import alerts as _alerts
                        _alerts.fire_and_forget(
                            kind="puller_error",
                            severity="error",
                            message=(
                                f"iLink puller {self.account.account_id} "
                                f"sustained failure: {exc}"
                            ),
                            dedup_key=f"puller_error:{self.account.account_id}",
                        )
                    try:
                        await asyncio.wait_for(self._stop.wait(), timeout=backoff)
                        return  # stop requested during backoff
                    except asyncio.TimeoutError:
                        pass
                    backoff = min(backoff * 2, RECONNECT_MAX_BACKOFF_S)

    async def _poll_once(self, client: httpx.AsyncClient) -> None:
        url = f"{self.account.base_url.rstrip('/')}/{EP_GET_UPDATES}"
        body_dict: dict[str, Any] = {"limit": DEFAULT_LIMIT, "base_info": {"channel_version": "2.2.0"}}
        if self._sync_buf:
            body_dict["sync_buf"] = self._sync_buf
        body = json.dumps(body_dict, ensure_ascii=False)

        # Construct headers the same way the praxagent client does for
        # POST. Re-import lazily so this module stays importable even if
        # praxagent isn't installed in the user's env (tests).
        from prax.integrations.wechat_ilink.client import _headers  # type: ignore

        loop = asyncio.get_running_loop()
        t0 = loop.time()
        resp = await client.post(
            url,
            content=body.encode("utf-8"),
            headers=_headers(self.account.token, body),
        )
        elapsed = loop.time() - t0
        # Use print() so it surfaces under uvicorn's default stderr capture
        # without depending on log-config plumbing. Switch to logger once
        # we wire up structured logging in Phase 5.
        print(
            f"[ilink_pull] {self.account.account_id} poll {elapsed:.2f}s "
            f"status={resp.status_code} body_len={len(resp.content)} "
            f"sync_buf_len={len(self._sync_buf)}",
            flush=True,
        )
        self.stats.polls_total += 1
        self.stats.last_poll_at = loop.time()

        if resp.status_code >= 400:
            raise RuntimeError(f"getupdates HTTP {resp.status_code}: {resp.text[:200]}")

        try:
            data = resp.json()
        except Exception as exc:
            raise RuntimeError(f"getupdates non-JSON response: {exc}; body={resp.text[:200]}")

        ret = data.get("ret")
        if ret is not None and ret != 0:
            errmsg = data.get("errmsg") or data.get("msg") or ""
            raise RuntimeError(f"getupdates ret={ret} errmsg={errmsg!r}")

        new_sync_buf = data.get("sync_buf")
        msgs = data.get("msgs") or []

        if msgs:
            logger.info(
                "ilink_pull[%s]: received %d msgs",
                self.account.account_id, len(msgs),
            )
        for m in msgs:
            await self._handle_msg(m)

        # Advance cursor only after successful processing.
        if new_sync_buf and new_sync_buf != self._sync_buf:
            self._sync_buf = new_sync_buf

    async def _handle_msg(self, msg: dict[str, Any]) -> None:
        loop = asyncio.get_running_loop()
        self.stats.msgs_total += 1
        self.stats.last_msg_at = loop.time()

        # Surface raw payload at debug level — invaluable while we're
        # still learning iLink's message shape from real traffic.
        logger.debug("ilink_pull[%s]: raw msg=%s", self.account.account_id, msg)

        from_user = msg.get("from_user_id") or ""
        text = _extract_text(msg)
        if not from_user or not text:
            # Non-text events (system notifications, group invites, etc.)
            # — log and skip; bridge.inbound only handles text replies.
            logger.info(
                "ilink_pull[%s]: skipping non-text msg (from=%s text_len=%d)",
                self.account.account_id, from_user, len(text),
            )
            return

        # Feed into bridge.inbound. bot_wxid is the bot's own user_id,
        # which iLink used as the sender wxid for outbound messages —
        # that's also what bindings.target_wxid stores against the user.
        bridge = self._bridge
        if bridge is None:
            from . import inbound as _inbound  # noqa: F401
            from .. import bridge as _bridge_pkg  # type: ignore
            bridge = _bridge_pkg  # whole package

        try:
            with bridge_connect(self.cwd) as conn:
                result = inbound_mod.receive(
                    conn,
                    bot_wxid=self.account.user_id,
                    target_wxid=from_user,
                    content=text,
                )
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "ilink_pull[%s]: inbound.receive failed: %s",
                self.account.account_id, exc,
            )
            self._record_error(f"inbound.receive: {exc}")
            return

        if result.get("matched"):
            self.stats.msgs_routed += 1
            logger.info(
                "ilink_pull[%s]: msg routed → app_user_id=%s persona=%s reason=%s",
                self.account.account_id,
                result.get("app_user_id"), result.get("persona_id"),
                result.get("reason"),
            )
            # Hand off to the application-layer callback (e.g. daily_qa).
            # Binding-claim events (kind == 'binding_claim') skip the app
            # layer — they're internal to bridge and the user already
            # received a welcome reply from the binding flow.
            if (
                self._inbound_callback is not None
                and result.get("kind") != "binding_claim"
            ):
                try:
                    with bridge_connect(self.cwd) as conn2:
                        await self._inbound_callback(
                            conn2,
                            cwd=self.cwd,
                            app_user_id=result["app_user_id"],
                            content=text,
                        )
                except Exception as exc:  # noqa: BLE001
                    logger.exception(
                        "ilink_pull[%s]: inbound_callback failed: %s",
                        self.account.account_id, exc,
                    )
                    self._record_error(f"inbound_callback: {exc}")
        else:
            self.stats.msgs_unmatched += 1
            logger.info(
                "ilink_pull[%s]: msg from %s unmatched (no binding) — content=%r",
                self.account.account_id, from_user, text[:80],
            )

    def _record_error(self, err: str) -> None:
        loop = asyncio.get_running_loop()
        self.stats.last_error = err
        self.stats.last_error_at = loop.time()


class PullerManager:
    """Owns the set of ILinkPullers — one per logged-in iLink account.

    The list of accounts can change at runtime (new login, account
    deletion). Call ``sync_with_accounts`` periodically (or after every
    /api/wechat/login confirmed) to bring the puller set in line.
    """

    def __init__(self, cwd, *, inbound_callback=None):
        self.cwd = Path(str(cwd))
        self.pullers: dict[str, ILinkPuller] = {}
        # Same callback semantics as ILinkPuller.__init__; new pullers
        # spawned via sync_with_accounts inherit this.
        self.inbound_callback = inbound_callback

    async def sync_with_accounts(self) -> dict[str, str]:
        """Reconcile pullers against currently logged-in iLink accounts.

        Returns a dict ``{account_id: action}`` where action is
        ``"started"`` / ``"stopped"`` / ``"unchanged"``.
        """
        from prax.integrations.wechat_ilink import list_accounts  # type: ignore

        try:
            accounts = list(list_accounts())
        except Exception as exc:  # noqa: BLE001
            logger.warning("PullerManager: list_accounts failed: %s", exc)
            return {}

        actions: dict[str, str] = {}
        active_ids = {a.account_id for a in accounts}

        # Stop pullers whose accounts were removed.
        for aid in list(self.pullers):
            if aid not in active_ids:
                p = self.pullers.pop(aid)
                await p.stop()
                actions[aid] = "stopped"
                logger.info("PullerManager: stopped puller %s", aid)

        # Start pullers for newly-seen accounts.
        for a in accounts:
            if a.account_id not in self.pullers:
                p = ILinkPuller(self.cwd, a, inbound_callback=self.inbound_callback)
                self.pullers[a.account_id] = p
                await p.start()
                actions[a.account_id] = "started"
                logger.info("PullerManager: started puller %s (user_id=%s)",
                            a.account_id, a.user_id)
            else:
                actions.setdefault(a.account_id, "unchanged")

        return actions

    async def stop_all(self) -> None:
        if not self.pullers:
            return
        await asyncio.gather(
            *(p.stop() for p in self.pullers.values()),
            return_exceptions=True,
        )
        self.pullers.clear()

    def status(self) -> list[dict[str, Any]]:
        out = []
        for aid, p in self.pullers.items():
            s = p.stats
            out.append({
                "account_id": aid,
                "user_id": p.account.user_id,
                "running": p.is_running,
                "polls_total": s.polls_total,
                "msgs_total": s.msgs_total,
                "msgs_routed": s.msgs_routed,
                "msgs_unmatched": s.msgs_unmatched,
                "last_error": s.last_error,
            })
        return out


# Helper because contextlib.suppress isn't async-friendly.
class contextlib_suppress:
    def __enter__(self):  # noqa: D401 - dunder
        return self
    def __exit__(self, exc_type, exc, tb):
        return True
