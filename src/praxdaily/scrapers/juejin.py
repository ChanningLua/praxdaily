"""掘金 scraper — recommend_all_feed POST API。

API: POST ``https://api.juejin.cn/recommend_api/v1/article/recommend_all_feed``
公开、零认证（仅个人化推荐 logged-out 时降级为热门）。

Body 参数：
  - ``sort_type=200``  → 热门（``300`` 是最新；``100`` 是默认推荐）
  - ``cursor="0"``     → 翻页起点
  - ``limit``          → 一页几条
  - ``id_type=2`` / ``client_type=2608`` 是 web 客户端固定值

Metric 用 ``digg_count``（点赞数）—— 比 view_count 更接近"质量信号"
（view 容易被引流刷起来；点赞要点击才算）。

返回的 article_info 中字段非常多，我们只取 url / title / 计数三件套。
"""

from __future__ import annotations

import json
import logging

import httpx

from .types import Item


logger = logging.getLogger(__name__)


_FEED_URL = "https://api.juejin.cn/recommend_api/v1/article/recommend_all_feed"
_USER_AGENT = "Mozilla/5.0 (praxdaily-scraper/0.7)"


def scrape(limit: int = 30, *, timeout: float = 10.0) -> list[Item]:
    """Fetch top hot articles from 掘金 recommend feed.

    Returns up to ``limit`` items, sorted by ``digg_count`` desc.
    """
    headers = {"User-Agent": _USER_AGENT, "Content-Type": "application/json"}
    body = {
        "id_type": 2,
        "client_type": 2608,
        "sort_type": 200,        # 热门
        "cursor": "0",
        "limit": limit,
    }
    with httpx.Client(timeout=timeout, headers=headers) as c:
        try:
            r = c.post(_FEED_URL, content=json.dumps(body))
            r.raise_for_status()
            data = r.json()
        except Exception as exc:  # noqa: BLE001
            logger.warning("juejin: feed fetch failed: %s", exc)
            return []

    if data.get("err_no") != 0:
        logger.warning("juejin: API err_no=%s err_msg=%s",
                       data.get("err_no"), data.get("err_msg"))
        return []

    items: list[Item] = []
    for entry in (data.get("data") or []):
        info = (entry.get("item_info") or {}).get("article_info") or {}
        if not info.get("article_id"):
            continue
        items.append(_to_item(entry))

    # Sort by digg_count desc so top_n cap downstream picks best.
    items.sort(key=lambda i: i.metric, reverse=True)
    return items[:limit]


def _to_item(entry: dict) -> Item:
    info = (entry.get("item_info") or {}).get("article_info") or {}
    user = (entry.get("item_info") or {}).get("author_user_info") or {}
    aid = str(info.get("article_id") or "")
    return Item(
        source="juejin",
        id=aid,
        title=str(info.get("title") or "(untitled)"),
        url=f"https://juejin.cn/post/{aid}",
        metric=int(info.get("digg_count") or 0),
        metric_label="digg",
        author=str(user.get("user_name") or ""),
        extra={
            "view_count": info.get("view_count"),
            "comment_count": info.get("comment_count"),
            "collect_count": info.get("collect_count"),
            "brief": (info.get("brief_content") or "")[:200],
        },
    )
