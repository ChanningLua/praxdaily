"""Bridge layer tests — persona/binding storage, broadcast fan-out,
inbound binding-token claim, daily_qa command routing.

These hit a real SQLite file (per-test temp dir) and stub
``prax.tools.notify.build_provider`` so no real iLink calls happen.
"""

from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path
from typing import Any

import pytest


# ── Test-time fake provider ───────────────────────────────────────────────


class FakeProvider:
    """Stand-in for ``wechat_personal``. Records every send for assertions
    and supports per-recipient failure injection."""

    sent: list[tuple[str, str, str]] = []  # (account_id, to, body)
    fail_to_wxid: str | None = None

    def __init__(self, cfg: dict):
        self.cfg = cfg

    async def send(self, *, title: str, body: str, level: str) -> None:  # noqa: D401
        if FakeProvider.fail_to_wxid == self.cfg.get("to"):
            raise RuntimeError("simulated iLink failure")
        FakeProvider.sent.append((self.cfg["account_id"], self.cfg["to"], body))


@pytest.fixture(autouse=True)
def install_fake_provider():
    """Install fake into ``sys.modules`` so ``import prax.tools.notify``
    inside bridge code returns our stub. Also attach to the parent
    ``prax.tools`` package so attribute access from ``monkeypatch.setattr``
    keeps working for legacy pipeline tests."""
    fake = types.ModuleType("prax.tools.notify")
    fake.build_provider = FakeProvider  # type: ignore[attr-defined]
    prev_module = sys.modules.get("prax.tools.notify")
    sys.modules["prax.tools.notify"] = fake
    parent = sys.modules.get("prax.tools")
    prev_parent_attr = getattr(parent, "notify", None) if parent is not None else None
    if parent is not None:
        parent.notify = fake  # type: ignore[attr-defined]
    FakeProvider.sent = []
    FakeProvider.fail_to_wxid = None
    yield
    # Restore so later tests see the real (or absent) module.
    if prev_module is None:
        sys.modules.pop("prax.tools.notify", None)
    else:
        sys.modules["prax.tools.notify"] = prev_module
    if parent is not None:
        if prev_parent_attr is None:
            try:
                delattr(parent, "notify")
            except AttributeError:
                pass
        else:
            parent.notify = prev_parent_attr  # type: ignore[attr-defined]


@pytest.fixture
def bridge_cwd(tmp_path: Path) -> Path:
    (tmp_path / ".prax").mkdir()
    return tmp_path


# ── Persona / binding CRUD ─────────────────────────────────────────────────


def test_persona_upsert_and_default(bridge_cwd: Path):
    from praxdaily.bridge import connect, personas

    with connect(bridge_cwd) as conn:
        p1 = personas.upsert(conn, persona_id="daily", name="日报", is_default=True)
        p2 = personas.upsert(conn, persona_id="other", name="其他")
        assert personas.get_default(conn).persona_id == "daily"
        # Setting another as default flips the previous.
        personas.upsert(conn, persona_id="other", name="其他", is_default=True)
        assert personas.get_default(conn).persona_id == "other"


def test_binding_unique_active_per_user(bridge_cwd: Path):
    from praxdaily.bridge import connect, personas, bots, bindings

    with connect(bridge_cwd) as conn:
        personas.upsert(conn, persona_id="daily", name="日报")
        bots.register(conn, wxid="bot1", persona_id="daily", ilink_account_id="acc1")
        b1 = bindings.manual_bind(
            conn, app_user_id="alice", persona_id="daily",
            target_wxid="wxid_alice",
        )
        # Re-binding the same user supersedes the prior row.
        b2 = bindings.manual_bind(
            conn, app_user_id="alice", persona_id="daily",
            target_wxid="wxid_alice_v2",
        )
        active = bindings.list_active(conn)
        assert len(active) == 1
        assert active[0].binding_id == b2.binding_id
        assert active[0].target_wxid == "wxid_alice_v2"


# ── Broadcast ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_broadcast_sends_chunks_to_every_user(bridge_cwd: Path):
    from praxdaily.bridge import connect, personas, bots, bindings, broadcast

    with connect(bridge_cwd) as conn:
        personas.upsert(conn, persona_id="daily", name="日报")
        bots.register(conn, wxid="bot1", persona_id="daily", ilink_account_id="acc1")
        for u in ("alice", "bob", "carol"):
            bindings.manual_bind(
                conn, app_user_id=u, persona_id="daily",
                target_wxid=f"wxid_{u}",
            )

        summary = await broadcast.broadcast_chunks(
            conn,
            chunks=["hello", "world"],
            inter_chunk_delay_s=0,
            inter_user_delay_s=0,
            apply_rate_limit=False,
        )
    assert summary["users_total"] == 3
    assert summary["users_sent"] == 3
    assert summary["chunks_sent"] == 6
    assert len(FakeProvider.sent) == 6


@pytest.mark.asyncio
async def test_broadcast_idempotency_replay_skips_network(bridge_cwd: Path):
    from praxdaily.bridge import connect, personas, bots, bindings, broadcast

    with connect(bridge_cwd) as conn:
        personas.upsert(conn, persona_id="daily", name="日报")
        bots.register(conn, wxid="bot1", persona_id="daily", ilink_account_id="acc1")
        bindings.manual_bind(
            conn, app_user_id="alice", persona_id="daily", target_wxid="wxid_alice",
        )

        await broadcast.broadcast_chunks(
            conn, chunks=["x"], idempotency_prefix="d-2026-05-07",
            inter_chunk_delay_s=0, inter_user_delay_s=0, apply_rate_limit=False,
        )
        first_sent = list(FakeProvider.sent)
        FakeProvider.sent.clear()
        await broadcast.broadcast_chunks(
            conn, chunks=["x"], idempotency_prefix="d-2026-05-07",
            inter_chunk_delay_s=0, inter_user_delay_s=0, apply_rate_limit=False,
        )
    assert len(first_sent) == 1
    assert FakeProvider.sent == []  # replay didn't hit network


@pytest.mark.asyncio
async def test_broadcast_failure_is_isolated(bridge_cwd: Path):
    from praxdaily.bridge import connect, personas, bots, bindings, broadcast

    with connect(bridge_cwd) as conn:
        personas.upsert(conn, persona_id="daily", name="日报")
        bots.register(conn, wxid="bot1", persona_id="daily", ilink_account_id="acc1")
        for u in ("alice", "bob", "carol"):
            bindings.manual_bind(
                conn, app_user_id=u, persona_id="daily",
                target_wxid=f"wxid_{u}",
            )
        FakeProvider.fail_to_wxid = "wxid_bob"
        summary = await broadcast.broadcast_chunks(
            conn, chunks=["msg"], idempotency_prefix="t",
            inter_chunk_delay_s=0, inter_user_delay_s=0, apply_rate_limit=False,
        )
    assert summary["users_sent"] == 2
    assert summary["users_failed"] == 1
    assert summary["failures"][0]["app_user_id"] == "bob"


# ── Binding-token claim via webhook ───────────────────────────────────────


def test_binding_token_create_and_claim(bridge_cwd: Path):
    from praxdaily.bridge import connect, personas, bots, binding_tokens, bindings, inbound

    with connect(bridge_cwd) as conn:
        personas.upsert(conn, persona_id="daily", name="日报")
        bots.register(conn, wxid="bot1", persona_id="daily", ilink_account_id="acc1")

        bt = binding_tokens.create(
            conn, app_user_id="alice", persona_id="daily",
        )
        # User sends a message containing the token: webhook captures it.
        result = inbound.receive(
            conn, bot_wxid="bot1", target_wxid="wxid_alice",
            content=f"binding code: {bt.token}",
        )
        assert result["matched"] is True
        assert result["kind"] == "binding_claim"
        assert result["app_user_id"] == "alice"

        # And the binding is now active.
        b = bindings.get_active(conn, app_user_id="alice", persona_id="daily")
        assert b.target_wxid == "wxid_alice"


# ── daily_qa command routing ──────────────────────────────────────────────


def test_daily_qa_parse_command():
    from praxdaily.apps.daily_qa import parse_command

    assert parse_command("今天") == ("today", "")
    assert parse_command("today") == ("today", "")
    assert parse_command("昨天") == ("yesterday", "")
    assert parse_command("查 OpenAI") == ("search", "OpenAI")
    assert parse_command("search GPT-5") == ("search", "GPT-5")
    assert parse_command("退订") == ("unsubscribe", "")
    assert parse_command("帮助") == ("help", "")
    assert parse_command("?") == ("help", "")
    assert parse_command("随便") == ("other", "随便")
    assert parse_command("") == ("other", "")


def test_daily_qa_search_recent(tmp_path: Path):
    from praxdaily.apps.daily_qa import search_recent
    from datetime import datetime

    (tmp_path / ".prax" / "vault" / datetime.now().date().isoformat()).mkdir(
        parents=True
    )
    digest = (
        "📅 today\n\n📰 HackerNews\n———————\n"
        "1. OpenAI launches GPT-5\n   by alice · 🔥 100 分\n   🔗 https://x.com\n\n"
        "2. Other news\n   by bob · 🔥 50 分\n   🔗 https://y.com\n"
    )
    digest_path = (
        tmp_path / ".prax" / "vault" / datetime.now().date().isoformat()
        / "daily-digest.md"
    )
    digest_path.write_text(digest, encoding="utf-8")

    hits = search_recent(tmp_path, keyword="OpenAI", days=2)
    assert len(hits) == 1
    assert "GPT-5" in hits[0]["title"]
    assert hits[0]["url"] == "https://x.com"

    assert search_recent(tmp_path, keyword="nonexistent", days=2) == []


@pytest.mark.asyncio
async def test_daily_qa_handle_unsubscribe_replies_first(bridge_cwd: Path):
    """Replies to '退订' must be sent BEFORE the binding is expired,
    otherwise the goodbye message can't be delivered."""
    from praxdaily.bridge import connect, personas, bots, bindings
    from praxdaily.apps import daily_qa

    with connect(bridge_cwd) as conn:
        personas.upsert(conn, persona_id="daily", name="日报")
        bots.register(conn, wxid="bot1", persona_id="daily", ilink_account_id="acc1")
        bindings.manual_bind(
            conn, app_user_id="alice", persona_id="daily", target_wxid="wxid_alice",
        )

        result = await daily_qa.handle(
            conn, cwd=bridge_cwd, app_user_id="alice", content="退订",
        )
    assert result["command"] == "unsubscribe"
    assert result["reply_sent"] is True
    assert any("已退订" in body for _, _, body in FakeProvider.sent)
    # And after the reply, binding should be expired.
    with connect(bridge_cwd) as conn:
        assert bindings.list_for_user(conn, "alice") == []


# ── Rate limiter ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_per_user_rate_limit_blocks_then_releases():
    """The 4th send for the same user within 1s should wait for slots."""
    import time
    from praxdaily.bridge.rate_limit import PerUserSlidingWindow

    rl = PerUserSlidingWindow(max_events=2, window_s=0.3)
    t0 = time.monotonic()
    await rl.acquire("u1")
    await rl.acquire("u1")
    await rl.acquire("u1")  # this should wait ~0.3s
    elapsed = time.monotonic() - t0
    assert elapsed >= 0.25, f"expected throttling but only took {elapsed}s"


@pytest.mark.asyncio
async def test_token_bucket_allows_burst():
    import time
    from praxdaily.bridge.rate_limit import TokenBucket

    tb = TokenBucket(rate=10.0, burst=5)
    t0 = time.monotonic()
    for _ in range(5):
        await tb.acquire(1)
    burst_elapsed = time.monotonic() - t0
    assert burst_elapsed < 0.05, f"burst should be near-instant, got {burst_elapsed}s"
    # 6th should wait ~0.1s for a refill.
    await tb.acquire(1)
    total_elapsed = time.monotonic() - t0
    assert total_elapsed >= 0.05
