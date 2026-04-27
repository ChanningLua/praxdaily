"""HackerNews scraper via the official Firebase API.

API ref: https://github.com/HackerNews/API — public, no auth, very stable.

We hit ``topstories.json`` for the list of IDs, then fan out to
``item/<id>.json`` for the top N. The API returns IDs sorted by
position on the front page, so taking the first N is the right
"hot" sample.
"""

from __future__ import annotations

import httpx

from .types import Item


_TOP_URL = "https://hacker-news.firebaseio.com/v0/topstories.json"
_ITEM_URL = "https://hacker-news.firebaseio.com/v0/item/{id}.json"


def scrape(limit: int = 20, *, timeout: float = 10.0) -> list[Item]:
    """Fetch top ``limit`` HN front-page stories.

    Returns whatever the API gives us; downstream filtering by keywords
    happens in the pipeline, not here. We do skip non-story types
    (jobs/polls/comments) since they pollute a "news" digest.
    """
    with httpx.Client(timeout=timeout) as c:
        r = c.get(_TOP_URL)
        r.raise_for_status()
        ids = r.json()[: max(limit * 2, limit)]  # over-fetch in case of skips

        items: list[Item] = []
        for sid in ids:
            if len(items) >= limit:
                break
            r = c.get(_ITEM_URL.format(id=sid))
            if r.status_code != 200:
                continue
            data = r.json() or {}
            if data.get("type") != "story" or data.get("dead") or data.get("deleted"):
                continue
            items.append(_to_item(data))
        return items


def _to_item(data: dict) -> Item:
    item_id = str(data.get("id", ""))
    return Item(
        source="hackernews",
        id=item_id,
        title=str(data.get("title") or "(untitled)"),
        # Self-posts (Ask HN / Show HN) have no `url` — link to the HN comment thread instead
        url=str(data.get("url") or f"https://news.ycombinator.com/item?id={item_id}"),
        metric=int(data.get("score") or 0),
        metric_label="score",
        author=str(data.get("by") or ""),
        extra={
            "descendants": data.get("descendants", 0),
            "time": data.get("time"),
        },
    )
