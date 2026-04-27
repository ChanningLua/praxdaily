"""Scraper unit tests — mock HTTP, lock in normalisation invariants.

We don't hit real APIs here; that's covered by occasional manual smoke
runs. The point of these tests is to catch the day someone tweaks
``_to_item`` and silently breaks downstream filtering / rendering.
"""

from __future__ import annotations

import pytest

from praxdaily.scrapers import hn, bilibili
from praxdaily.scrapers.types import Item


# ── HackerNews ──────────────────────────────────────────────────────────────


def test_hn_to_item_keeps_external_url(monkeypatch):
    """External link items: url should be the article URL, not a HN comment page."""
    raw = {
        "id": 12345, "type": "story", "title": "Cool article",
        "url": "https://example.com/article", "score": 250, "by": "alice",
        "descendants": 42, "time": 1700000000,
    }
    item = hn._to_item(raw)
    assert item.source == "hackernews"
    assert item.id == "12345"
    assert item.title == "Cool article"
    assert item.url == "https://example.com/article"
    assert item.metric == 250
    assert item.metric_label == "score"
    assert item.author == "alice"


def test_hn_to_item_self_post_links_to_thread():
    """Ask HN / Show HN have no `url` — must link to the comment thread
    so the user can still click through."""
    raw = {"id": 9999, "type": "story", "title": "Ask HN: X?", "score": 50, "by": "bob"}
    item = hn._to_item(raw)
    assert item.url == "https://news.ycombinator.com/item?id=9999"


def test_hn_scrape_skips_non_story_types(monkeypatch):
    """Top-stories list mixes in jobs/polls; the digest only wants stories."""
    calls = {"top": 0, "items": []}

    def fake_get(self, url):
        class R:
            status_code = 200
            def __init__(self, body): self._body = body
            def raise_for_status(self): pass
            def json(self): return self._body

        if url.endswith("/topstories.json"):
            calls["top"] += 1
            return R([1, 2, 3, 4])
        # /item/<id>.json
        sid = int(url.rsplit("/", 1)[-1].split(".")[0])
        calls["items"].append(sid)
        bodies = {
            1: {"id": 1, "type": "job", "title": "We're hiring", "score": 100},
            2: {"id": 2, "type": "story", "title": "Real story", "score": 80, "by": "u"},
            3: {"id": 3, "type": "story", "title": "Dead", "score": 50, "by": "u", "dead": True},
            4: {"id": 4, "type": "story", "title": "Other story", "score": 40, "by": "u"},
        }
        return R(bodies.get(sid, {}))

    import httpx
    monkeypatch.setattr(httpx.Client, "get", fake_get)

    items = hn.scrape(limit=2)
    titles = [i.title for i in items]
    assert "Real story" in titles
    assert "Other story" in titles
    assert "We're hiring" not in titles  # job filtered
    assert "Dead" not in titles           # dead filtered


# ── B 站 ─────────────────────────────────────────────────────────────────────


def test_bilibili_to_item_extracts_view_count():
    raw = {
        "bvid": "BV1xx", "title": "测试视频",
        "owner": {"name": "测试 UP"},
        "stat": {"view": 123456, "danmaku": 100, "like": 800},
    }
    item = bilibili._to_item(raw)
    assert item.id == "BV1xx"
    assert item.url == "https://www.bilibili.com/video/BV1xx"
    assert item.metric == 123456
    assert item.metric_label == "view"
    assert item.author == "测试 UP"


def test_bilibili_scrape_raises_on_api_error_code(monkeypatch):
    """B 站 wraps errors in {code, message}. We surface them so upstream
    sees the real reason instead of a generic empty list."""
    class R:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return {"code": -412, "message": "请求被拦截"}

    import httpx
    def _fake_get(self, *a, **kw): return R()
    monkeypatch.setattr(httpx.Client, "get", _fake_get)

    with pytest.raises(RuntimeError, match="请求被拦截"):
        bilibili.scrape(limit=5)


def test_bilibili_scrape_caps_at_20(monkeypatch):
    """B 站 ps param caps at 20 — ensure we don't accidentally request 100."""
    captured: dict = {}
    class R:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return {"code": 0, "data": {"list": []}}

    import httpx
    def _fake_get(self, url, params=None):
        captured["params"] = params
        return R()
    monkeypatch.setattr(httpx.Client, "get", _fake_get)

    bilibili.scrape(limit=200)
    assert captured["params"]["ps"] == 20


def test_item_dataclass_default_extra_is_independent():
    """Defensive: each Item gets its own extra dict, no shared-mutable-default trap."""
    a = Item(source="x", id="1", title="t", url="u")
    b = Item(source="x", id="2", title="t", url="u")
    a.extra["foo"] = 1
    assert "foo" not in b.extra
