"""Pipeline tests — keyword filter, threshold, chunked render, push retry.

These mock the scrapers + provider so we're testing the orchestration
layer's logic, not external services. Real wire-format quirks
(WeChat truncation, iLink ret=-2) are surfaced through carefully shaped
provider stubs.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
import yaml

from praxdaily import pipeline
from praxdaily.scrapers.types import Item


# ── Keyword filter ──────────────────────────────────────────────────────────


def test_keyword_filter_word_boundary_for_short_ascii():
    """The exact bug we hit: AGI matched MAGIC, AI matched PAID. Short
    ASCII keywords MUST match at word boundaries."""
    fn = pipeline._matches_keywords
    assert fn("AI agent runs amok", include=["AI"], exclude=[]) is True
    assert fn("Magic the Gathering", include=["AGI"], exclude=[]) is False
    assert fn("Paid subscription model", include=["AI"], exclude=[]) is False
    assert fn("Open AI announces", include=["AI"], exclude=[]) is True


def test_keyword_filter_chinese_substring():
    """Chinese has no word boundaries — substring match is correct."""
    fn = pipeline._matches_keywords
    assert fn("OpenAI 发布大模型", include=["大模型"], exclude=[]) is True
    assert fn("游戏更新", include=["大模型"], exclude=[]) is False


def test_keyword_filter_exclude_wins_over_include():
    fn = pipeline._matches_keywords
    assert fn("AI gaming news", include=["AI"], exclude=["gaming"]) is False


def test_keyword_filter_empty_include_matches_everything_nonempty():
    """Defensive: empty include shouldn't silently drop everything —
    only exclude rules apply."""
    fn = pipeline._matches_keywords
    assert fn("any random text", include=[], exclude=[]) is True


def test_keyword_filter_case_insensitive():
    fn = pipeline._matches_keywords
    assert fn("OPENAI launches", include=["openai"], exclude=[]) is True
    assert fn("openai launches", include=["OpenAI"], exclude=[]) is True


# ── Metric formatting ──────────────────────────────────────────────────────


def test_human_count_chinese_万_format():
    assert pipeline._human_count(500) == "500"
    assert pipeline._human_count(12_345) == "1.2万"
    assert pipeline._human_count(987_654) == "98.8万"
    assert pipeline._human_count(1_234_567_890) == "12.3亿"


def test_format_metric_uses_emoji_for_known_labels():
    assert "🔥" in pipeline._format_metric("score", 100)
    assert "👁" in pipeline._format_metric("view", 1_500)
    assert pipeline._format_metric("score", 0) == ""        # zero metric → omit
    assert pipeline._format_metric("unknown", 50) == "unknown 50"  # fallback


# ── Chunked rendering ──────────────────────────────────────────────────────


def _items(source: str, n: int, base_score: int = 100) -> list[Item]:
    return [
        Item(source=source, id=str(i), title=f"{source} item {i}",
             url=f"https://example.com/{source}/{i}",
             metric=base_score - i, metric_label="score" if source == "hackernews" else "view")
        for i in range(n)
    ]


def test_render_chunks_one_section_per_chunk_plus_header_footer():
    """Layout invariant: each source becomes one chunk; header is first,
    footer is last."""
    chunks = pipeline._render_chunks("2026-04-27", {
        "hackernews": _items("hackernews", 3),
        "bilibili":   _items("bilibili", 2),
    })
    assert len(chunks) == 4   # header + HN + 抖音 + footer
    assert chunks[0].startswith("📅 AI 日报")
    assert "HackerNews" in chunks[1]
    assert "B 站热门" in chunks[2]
    assert "praxdaily" in chunks[-1]


def test_render_chunks_empty_state_is_one_chunk():
    chunks = pipeline._render_chunks("2026-04-27", {})
    assert len(chunks) == 1
    assert "今天各源在筛选词下都没有命中" in chunks[0]


def test_render_chunks_splits_oversized_section(monkeypatch):
    """If a single source exceeds the chunk budget, it must split into
    multiple chunks with `(续 N)` headers — never silently truncate."""
    monkeypatch.setattr(pipeline, "WECHAT_CHUNK_BUDGET", 200)
    big = _items("hackernews", 8, base_score=999)
    chunks = pipeline._render_chunks("2026-04-27", {"hackernews": big})
    # Header + multiple HN chunks + footer
    assert len(chunks) >= 4
    hn_chunks = [c for c in chunks if "HackerNews" in c]
    assert len(hn_chunks) >= 2
    assert any("续" in c for c in hn_chunks[1:])


def test_render_chunks_skip_disabled_source_with_no_items():
    """A source with kept=0 shouldn't get a stray empty section in the
    digest. Header TOC also shouldn't list it."""
    chunks = pipeline._render_chunks("2026-04-27", {
        "hackernews": _items("hackernews", 2),
        "bilibili": [],
    })
    assert "B 站" not in chunks[0]   # not in TOC
    assert all("B 站" not in c for c in chunks)  # no section either


# ── Pipeline run end-to-end ────────────────────────────────────────────────


def _setup_workspace(tmp_path: Path, *, sources: list[dict], keywords: dict | None = None):
    """Write a minimal .prax/ scaffold: sources.yaml + notify.yaml +
    cron.yaml so the pipeline can resolve a channel."""
    prax_dir = tmp_path / ".prax"
    prax_dir.mkdir()
    (prax_dir / "sources.yaml").write_text(yaml.safe_dump({
        "sources": sources,
        "keywords": keywords or {"include": ["AI"], "exclude": []},
    }), encoding="utf-8")
    (prax_dir / "notify.yaml").write_text(yaml.safe_dump({
        "channels": {
            "test-channel": {
                "provider": "feishu_webhook",
                "url": "https://example.com/hook",
            },
        },
    }), encoding="utf-8")
    (prax_dir / "cron.yaml").write_text(yaml.safe_dump({
        "jobs": [{"name": "daily", "schedule": "0 17 * * *", "prompt": "x",
                  "notify_channel": "test-channel"}],
    }), encoding="utf-8")


def _async_run(coro):
    """Sync test helper for awaiting the async pipeline."""
    return asyncio.new_event_loop().run_until_complete(coro)


def test_pipeline_skips_disabled_sources(tmp_path, monkeypatch):
    _setup_workspace(tmp_path, sources=[
        {"id": "hackernews", "enabled": False, "limit": 5, "top_n": 3, "min_metric": 0},
    ])
    monkeypatch.setattr(pipeline, "_resolve_channel", lambda cwd: None)  # no push needed

    result = _async_run(pipeline.run(cwd=tmp_path))
    sr = next(s for s in result.sources if s.id == "hackernews")
    assert sr.enabled is False
    assert sr.fetched == 0
    assert sr.kept == 0


def test_pipeline_min_metric_drops_low_score_items(tmp_path, monkeypatch):
    """The exact UX issue we fixed today: a 4-score item shouldn't
    appear next to a 400-score one."""
    _setup_workspace(
        tmp_path,
        sources=[{"id": "hackernews", "enabled": True, "limit": 10, "top_n": 10, "min_metric": 100}],
        keywords={"include": ["AI"], "exclude": []},
    )

    sample = [
        Item(source="hackernews", id="1", title="AI big news", url="x", metric=500, metric_label="score"),
        Item(source="hackernews", id="2", title="AI medium",   url="x", metric=150, metric_label="score"),
        Item(source="hackernews", id="3", title="AI tiny",     url="x", metric=4,   metric_label="score"),
    ]
    monkeypatch.setitem(pipeline.SCRAPERS, "hackernews", lambda limit: sample)
    monkeypatch.setattr(pipeline, "_resolve_channel", lambda cwd: None)

    result = _async_run(pipeline.run(cwd=tmp_path))
    sr = next(s for s in result.sources if s.id == "hackernews")
    assert sr.fetched == 3
    assert sr.kept == 2     # 4-score one filtered out by min_metric


def test_pipeline_top_n_caps_kept_after_keyword_filter(tmp_path, monkeypatch):
    _setup_workspace(
        tmp_path,
        sources=[{"id": "hackernews", "enabled": True, "limit": 10, "top_n": 2, "min_metric": 0}],
    )
    sample = [Item(source="hackernews", id=str(i), title=f"AI item {i}",
                   url="x", metric=1000 - i, metric_label="score") for i in range(5)]
    monkeypatch.setitem(pipeline.SCRAPERS, "hackernews", lambda limit: sample)
    monkeypatch.setattr(pipeline, "_resolve_channel", lambda cwd: None)

    result = _async_run(pipeline.run(cwd=tmp_path))
    sr = next(s for s in result.sources if s.id == "hackernews")
    assert sr.kept == 2  # only top 2 by score


def test_pipeline_per_source_scraper_failure_isolated(tmp_path, monkeypatch):
    """One source raising must not nuke others — captured into SourceResult."""
    _setup_workspace(tmp_path, sources=[
        {"id": "hackernews", "enabled": True, "limit": 5, "top_n": 3, "min_metric": 0},
        {"id": "bilibili",   "enabled": True, "limit": 5, "top_n": 3, "min_metric": 0},
    ])

    def boom(limit):
        raise RuntimeError("network fail")

    monkeypatch.setitem(pipeline.SCRAPERS, "hackernews", lambda limit: [
        Item(source="hackernews", id="1", title="AI good", url="x", metric=500, metric_label="score"),
    ])
    monkeypatch.setitem(pipeline.SCRAPERS, "bilibili", boom)
    monkeypatch.setattr(pipeline, "_resolve_channel", lambda cwd: None)

    result = _async_run(pipeline.run(cwd=tmp_path))
    hn_r = next(s for s in result.sources if s.id == "hackernews")
    bi_r = next(s for s in result.sources if s.id == "bilibili")
    assert hn_r.kept == 1 and hn_r.error == ""
    assert bi_r.kept == 0 and "network fail" in bi_r.error


def test_pipeline_writes_digest_file_and_records_path(tmp_path, monkeypatch):
    _setup_workspace(tmp_path, sources=[
        {"id": "hackernews", "enabled": True, "limit": 5, "top_n": 3, "min_metric": 0},
    ])
    monkeypatch.setitem(pipeline.SCRAPERS, "hackernews", lambda limit: [
        Item(source="hackernews", id="1", title="AI yes", url="x", metric=500, metric_label="score"),
    ])
    monkeypatch.setattr(pipeline, "_resolve_channel", lambda cwd: None)

    result = _async_run(pipeline.run(cwd=tmp_path))
    digest = Path(result.digest_path)
    assert digest.exists()
    body = digest.read_text(encoding="utf-8")
    assert "AI yes" in body
    assert result.digest_chars == len(body)


# ── Channel resolution ─────────────────────────────────────────────────────


def test_resolve_channel_prefers_cron_job_hint(tmp_path):
    _setup_workspace(tmp_path, sources=[])
    # cron job points at "test-channel" — it should be picked even though
    # there are other channels.
    notify_path = tmp_path / ".prax" / "notify.yaml"
    notify_path.write_text(yaml.safe_dump({
        "channels": {
            "wechat-self": {"provider": "wechat_personal", "account_id": "abc"},
            "test-channel": {"provider": "feishu_webhook", "url": "https://x"},
        },
    }), encoding="utf-8")
    name, cfg = pipeline._resolve_channel(tmp_path)
    assert name == "test-channel"


def test_resolve_channel_falls_back_to_first_wechat_personal(tmp_path):
    """No cron hint → prefer wechat (most users want this)."""
    prax_dir = tmp_path / ".prax"
    prax_dir.mkdir()
    (prax_dir / "notify.yaml").write_text(yaml.safe_dump({
        "channels": {
            "feishu-thing": {"provider": "feishu_webhook", "url": "https://x"},
            "my-wechat":    {"provider": "wechat_personal", "account_id": "abc"},
        },
    }), encoding="utf-8")
    name, _ = pipeline._resolve_channel(tmp_path)
    assert name == "my-wechat"


def test_resolve_channel_returns_none_when_no_notify_yaml(tmp_path):
    assert pipeline._resolve_channel(tmp_path) is None


# ── Push retry ─────────────────────────────────────────────────────────────


class _RetryProvider:
    """Stub provider that succeeds on the Nth attempt for a given chunk."""

    def __init__(self, fail_first_n_per_chunk: dict[int, int] | int = 0):
        if isinstance(fail_first_n_per_chunk, int):
            self.fail_n = {i: fail_first_n_per_chunk for i in range(100)}
        else:
            self.fail_n = dict(fail_first_n_per_chunk)
        self.calls: list[str] = []
        self._chunk_idx = -1

    async def send(self, *, title, body, level):
        if not self.calls or self.calls[-1] != body:
            self._chunk_idx += 1
        self.calls.append(body)
        remaining = self.fail_n.get(self._chunk_idx, 0)
        if remaining > 0:
            self.fail_n[self._chunk_idx] = remaining - 1
            raise RuntimeError("iLink 拒收 (ret=-2): session lost")


def test_push_retries_chunk_on_transient_failure(monkeypatch):
    """The exact iLink quirk: ret=-2 between chunks. Retry should
    transparently recover, end up sent: True."""
    # Stub asyncio.sleep on the *pipeline* module's reference so the test
    # doesn't actually wait (and doesn't recurse: capture real coroutine).
    async def _no_sleep(_): return None
    monkeypatch.setattr(pipeline.asyncio, "sleep", _no_sleep)
    provider = _RetryProvider(fail_first_n_per_chunk={1: 1})  # chunk 2 fails once

    monkeypatch.setattr(
        "prax.tools.notify.build_provider",
        lambda cfg: provider,
    )
    result = _async_run(pipeline._push_chunks(
        "ch", {"provider": "feishu_webhook", "url": "https://x"},
        chunks=["A", "B", "C"],
    ))
    assert result["sent"] is True
    assert result["chunks_sent"] == 3
    # Chunk B was attempted twice; A and C once each → 4 total send calls.
    assert len(provider.calls) == 4


def test_push_gives_up_after_3_attempts_per_chunk(monkeypatch):
    async def _no_sleep(_): return None
    monkeypatch.setattr(pipeline.asyncio, "sleep", _no_sleep)
    # Fail every attempt forever
    provider = _RetryProvider(fail_first_n_per_chunk=99)
    monkeypatch.setattr(
        "prax.tools.notify.build_provider",
        lambda cfg: provider,
    )

    result = _async_run(pipeline._push_chunks(
        "ch", {"provider": "feishu_webhook", "url": "https://x"},
        chunks=["A", "B"],
    ))
    assert result["sent"] is False
    assert result["chunks_sent"] == 0
    assert "after retries" in result["error"]
