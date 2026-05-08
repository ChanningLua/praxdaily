"""Alert dispatch — push operational events to a webhook (飞书/钉钉/企微).

Subscribes to a single webhook URL configured via env var
``PRAXDAILY_ALERT_WEBHOOK``. When unset, all alert calls are silent
no-ops, so this module is safe to wire up from anywhere — you don't
have to gate calls on configuration.

Three event categories are wired in (see ``bridge/__init__.py`` and
``bridge/ilink_pull.py``):

  - ``puller_error``   — getupdates loop hit a recurring failure
  - ``send_failure``   — outbound retry exhausted (per binding)
  - ``content_blocked``— sensitive-word filter rejected an outbound msg

The webhook payload is the minimal shape that 飞书 / 钉钉 / 企微 群机器人
all accept: ``{"msg_type":"text","content":{"text": "..."}}``. If the
target webhook needs a different shape (e.g. signed 钉钉) the module
exposes ``_render_payload`` for monkeypatching.

In-memory dedupe over a 5-minute window prevents a noisy puller from
spamming the channel with the same error every 60s — the second+ hit
within the window is dropped silently (logged, not pushed).
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import OrderedDict
from typing import Any


logger = logging.getLogger(__name__)


WEBHOOK_ENV_VAR = "PRAXDAILY_ALERT_WEBHOOK"
DEDUP_WINDOW_S = 300.0
DEDUP_CACHE_MAX = 256
SEND_TIMEOUT_S = 5.0


# Module-level dedup cache: {dedup_key: last_sent_ts}. LRU-trimmed.
_dedup: "OrderedDict[str, float]" = OrderedDict()


def _webhook_url() -> str | None:
    url = os.environ.get(WEBHOOK_ENV_VAR, "").strip()
    return url or None


def _should_send(dedup_key: str) -> bool:
    """Return True if this alert should fire; updates the dedupe cache."""
    now = time.monotonic()
    last = _dedup.get(dedup_key)
    if last is not None and (now - last) < DEDUP_WINDOW_S:
        return False
    _dedup[dedup_key] = now
    _dedup.move_to_end(dedup_key)
    while len(_dedup) > DEDUP_CACHE_MAX:
        _dedup.popitem(last=False)
    return True


def _render_payload(*, severity: str, kind: str, message: str) -> dict[str, Any]:
    """Default payload — works for 飞书 / 钉钉 / 企微 default text webhook.

    Override by monkeypatching this function if you need a signed
    payload (钉钉 with secret) or a richer card layout.
    """
    icon = {"info": "ℹ", "warn": "⚠", "error": "🚨"}.get(severity, "•")
    text = f"{icon} praxdaily/{kind}\n{message}"
    return {"msg_type": "text", "content": {"text": text}}


async def send_alert(
    *,
    kind: str,
    message: str,
    severity: str = "warn",
    dedup_key: str | None = None,
) -> bool:
    """Fire an alert. Returns True if actually pushed, False on noop /
    dedupe / send failure (silent — alerts must never raise into caller).

    ``dedup_key`` defaults to ``kind`` when omitted, so all alerts of the
    same kind dedupe together. Pass a more specific key (e.g.
    ``f"puller:{account_id}"``) to allow independent dedup per source.
    """
    url = _webhook_url()
    if url is None:
        return False

    key = dedup_key or kind
    if not _should_send(key):
        logger.debug("alerts: deduped %s (within %ds window)", key, int(DEDUP_WINDOW_S))
        return False

    try:
        import httpx  # local import — alerts must work even if httpx is lazily installed
    except ImportError:
        logger.warning("alerts: httpx unavailable, skipping")
        return False

    payload = _render_payload(severity=severity, kind=kind, message=message)
    try:
        async with httpx.AsyncClient(timeout=SEND_TIMEOUT_S) as client:
            r = await client.post(url, json=payload)
            if r.status_code >= 400:
                logger.warning(
                    "alerts: webhook returned %d: %s",
                    r.status_code, r.text[:200],
                )
                return False
    except Exception as exc:  # noqa: BLE001
        logger.warning("alerts: webhook send failed: %s", exc)
        return False

    return True


def fire_and_forget(*args, **kwargs) -> None:
    """Schedule ``send_alert`` on the running event loop without
    blocking. Use this from sync code paths that have a loop available
    (e.g. inside a FastAPI request) — for fully sync paths fall back to
    spawning a thread or just calling logger.warning instead."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return  # no loop, drop the alert
    loop.create_task(send_alert(*args, **kwargs))
