"""In-memory rate limiters — protect against accidental flood + iLink risk-control.

Two flavors:

  - **Per-user sliding window**: caps how often a single ``app_user_id``
    can be sent to. Prevents a runaway loop or buggy retry from spamming
    one user. Default: 6 sends per 60 seconds.

  - **Global token bucket**: caps the total iLink call rate across all
    users. iLink risk-controls accounts that send too fast. Default:
    5 sends per second, burst 10.

Both are async-safe (asyncio.Lock) and used by ``bridge.broadcast`` and
``apps.daily_qa`` before each outbound network call. They're in-memory
only — process restart wipes counters, which is fine since the limits
exist to catch runaway behavior, not to enforce billing.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque


class PerUserSlidingWindow:
    """N events per ``window_s`` per key. Drops nothing — callers just
    block (await) until a slot is available."""

    def __init__(self, *, max_events: int = 6, window_s: float = 60.0):
        self.max_events = max_events
        self.window_s = window_s
        self._events: dict[str, deque[float]] = {}
        self._lock = asyncio.Lock()

    async def acquire(self, key: str) -> None:
        while True:
            async with self._lock:
                now = time.monotonic()
                dq = self._events.setdefault(key, deque())
                # Drop events older than window.
                while dq and dq[0] < now - self.window_s:
                    dq.popleft()
                if len(dq) < self.max_events:
                    dq.append(now)
                    return
                # Wait until the oldest event ages out.
                wait_s = (dq[0] + self.window_s) - now
            await asyncio.sleep(max(0.05, wait_s + 0.01))


class TokenBucket:
    """Classic token bucket — tokens refill at ``rate`` per second up to
    ``burst``. ``acquire`` blocks until a token is available."""

    def __init__(self, *, rate: float = 5.0, burst: int = 10):
        self.rate = rate
        self.burst = burst
        self._tokens: float = float(burst)
        self._last_refill: float = time.monotonic()
        self._lock = asyncio.Lock()

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(self.burst, self._tokens + elapsed * self.rate)
        self._last_refill = now

    async def acquire(self, n: int = 1) -> None:
        if n > self.burst:
            raise ValueError(f"requested {n} > burst {self.burst}")
        while True:
            async with self._lock:
                self._refill()
                if self._tokens >= n:
                    self._tokens -= n
                    return
                shortfall = n - self._tokens
                wait_s = shortfall / self.rate
            await asyncio.sleep(max(0.05, wait_s))


# ── Module-level singletons ───────────────────────────────────────────────

# These default values match the production-grade rationale documented in
# ``app-app-app-dreamy-thimble.md``: per-user 6/min protects users from
# spam; global 5/s avoids iLink risk-control thresholds.
PER_USER = PerUserSlidingWindow(max_events=6, window_s=60.0)
GLOBAL_ILINK = TokenBucket(rate=5.0, burst=10)


async def acquire_outbound(app_user_id: str) -> None:
    """One-stop helper: enforce both limiters before any outbound send."""
    await PER_USER.acquire(app_user_id)
    await GLOBAL_ILINK.acquire(1)
