"""Scraper unit tests — mock HTTP, lock in normalisation invariants.

We don't hit real APIs here; that's covered by occasional manual smoke
runs. The point of these tests is to catch the day someone tweaks
``_to_item`` and silently breaks downstream filtering / rendering.
"""

from __future__ import annotations

import pytest

from praxdaily.scrapers import hn, bilibili, reddit, zhihu_daily, juejin, segmentfault
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


# ── Reddit ─────────────────────────────────────────────────────────────────


def test_reddit_to_item_uses_permalink_for_url():
    """Reddit posts can be self-text or external links; we always link
    to the comment thread (permalink) so users can engage with the
    discussion + read external link from there."""
    raw = {
        "id": "abc123",
        "title": "Some AI breakthrough",
        "permalink": "/r/MachineLearning/comments/abc123/some_ai_breakthrough/",
        "url": "https://arxiv.org/abs/1234.5678",
        "ups": 542,
        "num_comments": 89,
    }
    item = reddit._to_item(raw, "MachineLearning")
    assert item.source == "reddit"
    assert item.id == "abc123"
    assert item.url == "https://www.reddit.com/r/MachineLearning/comments/abc123/some_ai_breakthrough/"
    assert item.metric == 542
    assert item.metric_label == "score"
    assert item.author == "r/MachineLearning"
    assert item.extra["external_url"] == "https://arxiv.org/abs/1234.5678"


def test_reddit_to_item_falls_back_to_score_field():
    """Some endpoints return ``score`` instead of ``ups``."""
    raw = {"id": "x", "title": "t", "permalink": "/r/x/", "score": 100}
    item = reddit._to_item(raw, "x")
    assert item.metric == 100


def test_reddit_scrape_skips_stickied_and_nsfw(monkeypatch):
    """Don't pollute a news digest with mod-pinned megathreads or NSFW."""
    fake_resp = {
        "data": {
            "children": [
                {"data": {"id": "1", "title": "stickied notice", "stickied": True, "ups": 999}},
                {"data": {"id": "2", "title": "nsfw post", "over_18": True, "ups": 500}},
                {"data": {"id": "3", "title": "real post", "permalink": "/r/x/3/", "ups": 100}},
            ]
        }
    }

    import httpx

    class R:
        status_code = 200
        def json(self): return fake_resp

    def _fake_get(self, url, params=None):
        return R()

    monkeypatch.setattr(httpx.Client, "get", _fake_get)
    items = reddit.scrape(limit=10, subreddits=["x"])
    assert len(items) == 1
    assert items[0].id == "3"


def test_reddit_scrape_merges_subs_and_sorts_by_score(monkeypatch):
    """When 2 subs return posts, the merged result is sorted by score
    so top_n cap downstream picks globally top items, not first-sub-bias."""
    sub_responses = {
        "subA": {"data": {"children": [
            {"data": {"id": "a1", "title": "low A", "permalink": "/r/subA/a1/", "ups": 10}},
            {"data": {"id": "a2", "title": "high A", "permalink": "/r/subA/a2/", "ups": 800}},
        ]}},
        "subB": {"data": {"children": [
            {"data": {"id": "b1", "title": "mid B", "permalink": "/r/subB/b1/", "ups": 200}},
        ]}},
    }

    import httpx

    class R:
        def __init__(self, body): self._body = body; self.status_code = 200
        def json(self): return self._body

    def _fake_get(self, url, params=None):
        for sub, body in sub_responses.items():
            if f"/r/{sub}/" in url:
                return R(body)
        return R({"data": {"children": []}})

    monkeypatch.setattr(httpx.Client, "get", _fake_get)
    items = reddit.scrape(limit=10, subreddits=["subA", "subB"])
    assert [i.id for i in items] == ["a2", "b1", "a1"]  # sorted desc


def test_reddit_scrape_per_sub_failure_isolated(monkeypatch):
    """One subreddit returning HTTP 503 must not abort the whole scrape."""
    import httpx

    class R:
        def __init__(self, status, body=None):
            self.status_code = status
            self._body = body or {"data": {"children": []}}
        def json(self): return self._body

    def _fake_get(self, url, params=None):
        if "broken_sub" in url:
            return R(503)
        return R(200, {"data": {"children": [
            {"data": {"id": "ok", "title": "ok post", "permalink": "/r/ok/ok/", "ups": 50}},
        ]}})

    monkeypatch.setattr(httpx.Client, "get", _fake_get)
    items = reddit.scrape(limit=10, subreddits=["broken_sub", "ok_sub"])
    assert len(items) == 1
    assert items[0].id == "ok"


# ── 知乎日报 ──────────────────────────────────────────────────────────────


def test_zhihu_daily_to_item_uses_position_as_metric():
    """编辑顺序就是热度信号——榜首 = 最高 metric，依次递减。"""
    story = {
        "id": 9789560,
        "title": "如何分步骤快速看懂上市公司年报？",
        "url": "https://daily.zhihu.com/story/9789560",
        "hint": "MR Dang · 4 分钟阅读",
        "images": ["https://pica.zhimg.com/x.jpg"],
    }
    item0 = zhihu_daily._to_item(story, position=0, total=5)
    item3 = zhihu_daily._to_item(story, position=3, total=5)
    assert item0.metric > item3.metric
    assert item0.source == "zhihu_daily"
    assert item0.metric_label == "curation"
    assert item0.author == "MR Dang"   # hint 第一段


def test_zhihu_daily_scrape_returns_empty_on_network_failure(monkeypatch):
    """网络失败必须降级为空列表，不能抛 unhandled 异常拖垮整个 pipeline。"""
    import httpx
    def _fake_get(self, url, **kw): raise httpx.ConnectTimeout("network down")
    monkeypatch.setattr(httpx.Client, "get", _fake_get)
    assert zhihu_daily.scrape(limit=5) == []


# ── 掘金 ────────────────────────────────────────────────────────────────


def test_juejin_to_item_extracts_digg_count():
    """metric_label='digg'（点赞数），url 用 article_id 拼接。"""
    entry = {
        "item_info": {
            "article_info": {
                "article_id": "7584110439933100078",
                "title": "深入理解 Transformer",
                "digg_count": 268,
                "view_count": 5400,
                "comment_count": 30,
                "collect_count": 80,
                "brief_content": "本文介绍...",
            },
            "author_user_info": {"user_name": "前端阿强"},
        }
    }
    item = juejin._to_item(entry)
    assert item.source == "juejin"
    assert item.id == "7584110439933100078"
    assert item.url == "https://juejin.cn/post/7584110439933100078"
    assert item.metric == 268
    assert item.metric_label == "digg"
    assert item.author == "前端阿强"
    assert item.extra["view_count"] == 5400


def test_juejin_scrape_handles_api_error_gracefully(monkeypatch):
    """API 返回 err_no != 0 时返回空，不应继续尝试解析。"""
    import httpx
    class R:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return {"err_no": 401, "err_msg": "auth required", "data": []}
    def _fake_post(self, url, content=None): return R()
    monkeypatch.setattr(httpx.Client, "post", _fake_post)
    assert juejin.scrape(limit=10) == []


def test_juejin_scrape_sorts_by_digg_desc(monkeypatch):
    """合并后必须按点赞数降序，让下游 top_n 能挑出最热。"""
    fake_data = {"err_no": 0, "data": [
        {"item_info": {"article_info": {"article_id": "1", "title": "low", "digg_count": 10}}},
        {"item_info": {"article_info": {"article_id": "2", "title": "high", "digg_count": 800}}},
        {"item_info": {"article_info": {"article_id": "3", "title": "mid", "digg_count": 200}}},
    ]}
    import httpx
    class R:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return fake_data
    monkeypatch.setattr(httpx.Client, "post", lambda self, url, content=None: R())
    items = juejin.scrape(limit=10)
    assert [i.id for i in items] == ["2", "3", "1"]


# ── SegmentFault ──────────────────────────────────────────────────────────


def test_segmentfault_parses_atom_entries(monkeypatch):
    """Atom feed 的 entry 必须正确解析为 Item，position 映射到 metric。"""
    fake_xml = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title type="text">SegmentFault</title>
  <entry>
    <title type="text">如何用 Vue 3 接入 LLM？</title>
    <link rel="alternate" type="text/html" href="https://segmentfault.com/q/123456" />
    <id>https://segmentfault.com/q/123456</id>
    <author><name>frontend_user</name></author>
    <summary>我在做一个聊天界面...</summary>
  </entry>
  <entry>
    <title type="text">第二条问题</title>
    <link rel="alternate" type="text/html" href="https://segmentfault.com/q/789012" />
    <id>https://segmentfault.com/q/789012</id>
    <author><name>another</name></author>
  </entry>
</feed>"""
    import httpx
    class R:
        status_code = 200
        text = fake_xml
        def raise_for_status(self): pass
    monkeypatch.setattr(httpx.Client, "get", lambda self, url, **kw: R())

    items = segmentfault.scrape(limit=10)
    assert len(items) == 2
    assert items[0].source == "segmentfault"
    assert items[0].id == "123456"
    assert items[0].title == "如何用 Vue 3 接入 LLM？"
    assert items[0].url == "https://segmentfault.com/q/123456"
    assert items[0].author == "frontend_user"
    # First entry must outrank the second by metric.
    assert items[0].metric > items[1].metric
    assert items[0].metric_label == "recency"


def test_segmentfault_skips_malformed_entry(monkeypatch):
    """缺 title 或 link 的 entry 应被跳过，不抛异常。"""
    fake_xml = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title type="text">good entry</title>
    <link rel="alternate" type="text/html" href="https://segmentfault.com/q/1" />
    <id>https://segmentfault.com/q/1</id>
  </entry>
  <entry>
    <id>https://segmentfault.com/q/2</id>
  </entry>
</feed>"""
    import httpx
    class R:
        status_code = 200
        text = fake_xml
        def raise_for_status(self): pass
    monkeypatch.setattr(httpx.Client, "get", lambda self, url, **kw: R())

    items = segmentfault.scrape(limit=10)
    assert len(items) == 1
    assert items[0].id == "1"


def test_item_dataclass_default_extra_is_independent():
    """Defensive: each Item gets its own extra dict, no shared-mutable-default trap."""
    a = Item(source="x", id="1", title="t", url="u")
    b = Item(source="x", id="2", title="t", url="u")
    a.extra["foo"] = 1
    assert "foo" not in b.extra
