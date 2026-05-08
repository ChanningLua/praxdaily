"""知乎日报 scraper — 用官方 mobile JSON API。

API: ``https://news-at.zhihu.com/api/4/news/latest`` 公开、零认证、稳定。
返回当天编辑精选的几条故事（已经是 curation 后的，全是 5-10 条精品）。

Metric 设计：知乎日报这套接口没有点赞/查看数，但**编辑排序本身**就是
最强的信号——榜首是当天最值得读的那条。所以 metric 取
``MAX_METRIC - index``，让第一条得分最高、依次递减；下游 ``top_n``
取前几条即可。
"""

from __future__ import annotations

import logging

import httpx

from .types import Item


logger = logging.getLogger(__name__)


_LATEST_URL = "https://news-at.zhihu.com/api/4/news/latest"
_USER_AGENT = "Mozilla/5.0 (praxdaily-scraper/0.7)"
_MAX_METRIC = 1000   # arbitrary; high enough so any sane min_metric still includes top items


def scrape(limit: int = 10, *, timeout: float = 10.0) -> list[Item]:
    """Fetch today's curated 知乎日报 stories."""
    headers = {"User-Agent": _USER_AGENT}
    with httpx.Client(timeout=timeout, headers=headers) as c:
        try:
            r = c.get(_LATEST_URL)
            r.raise_for_status()
            data = r.json()
        except Exception as exc:  # noqa: BLE001
            logger.warning("zhihu_daily: fetch failed: %s", exc)
            return []

    stories = (data.get("stories") or [])[:limit]
    items: list[Item] = []
    for idx, story in enumerate(stories):
        items.append(_to_item(story, position=idx, total=len(stories)))
    return items


def _to_item(story: dict, *, position: int, total: int) -> Item:
    sid = str(story.get("id", ""))
    return Item(
        source="zhihu_daily",
        id=sid,
        title=str(story.get("title") or "(untitled)"),
        url=str(story.get("url") or f"https://daily.zhihu.com/story/{sid}"),
        # Editor curation order = signal. First = highest metric.
        metric=max(_MAX_METRIC - position, 1),
        metric_label="curation",
        author=str(story.get("hint") or "知乎日报").split(" · ")[0],
        extra={
            "image": (story.get("images") or [None])[0],
            "hint": story.get("hint"),
            "position": position,
        },
    )
