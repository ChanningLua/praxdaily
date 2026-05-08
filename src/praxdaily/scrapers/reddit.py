"""Reddit scraper via the public ``.json`` endpoint — no auth.

Hits ``https://www.reddit.com/r/<sub>/top.json?t=day`` for each
subreddit in the configured list, pulls the top posts of the last 24h,
and returns them with ``score`` (Reddit ups) as the heat metric so
they sort alongside HN posts cleanly.

Reddit's ``.json`` endpoint is rate-limited unauthenticated — we send
a polite User-Agent and one small request per subreddit, well within
the unauthenticated quota for daily-batch use.

Default subs cover the highest-signal AI corners on Reddit:

  - r/LocalLLaMA — open-weight model news
  - r/MachineLearning — academic + applied ML
  - r/singularity — AGI / capability discussion

Override via ``.prax/sources.yaml``:

  - id: reddit
    enabled: true
    limit: 30
    top_n: 8
    min_metric: 100
    extra:
      subreddits: [LocalLLaMA, MachineLearning]
      time: day              # day | week | month
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from .types import Item


logger = logging.getLogger(__name__)


_DEFAULT_SUBREDDITS = ("LocalLLaMA", "MachineLearning", "singularity")
_DEFAULT_TIME = "day"
_USER_AGENT = "praxdaily-scraper/0.7 (+https://github.com/ChanningLua/praxdaily)"


def scrape(
    limit: int = 30,
    *,
    timeout: float = 10.0,
    subreddits: tuple[str, ...] | list[str] = _DEFAULT_SUBREDDITS,
    time: str = _DEFAULT_TIME,
) -> list[Item]:
    """Fetch top posts from each configured subreddit, merged + sorted by score.

    ``limit`` is the TOTAL across subs; we fan out evenly. With 3 subs
    and limit=30, we ask each for 10 posts.
    """
    subs = list(subreddits) or list(_DEFAULT_SUBREDDITS)
    per_sub = max(1, limit // len(subs))
    all_items: list[Item] = []

    headers = {"User-Agent": _USER_AGENT}
    with httpx.Client(timeout=timeout, headers=headers, follow_redirects=True) as c:
        for sub in subs:
            url = f"https://www.reddit.com/r/{sub}/top.json"
            try:
                r = c.get(url, params={"t": time, "limit": per_sub})
                if r.status_code != 200:
                    logger.warning(
                        "reddit: r/%s returned HTTP %d — skipping",
                        sub, r.status_code,
                    )
                    continue
                data = r.json()
            except Exception as exc:  # noqa: BLE001
                logger.warning("reddit: r/%s fetch failed: %s — skipping", sub, exc)
                continue

            children = (data.get("data") or {}).get("children") or []
            for child in children:
                post = child.get("data") or {}
                if post.get("stickied") or post.get("over_18"):
                    continue
                all_items.append(_to_item(post, sub))

    # Sort merged stream by score so the digest's top_n cap picks
    # the global top across sources.
    all_items.sort(key=lambda i: i.metric, reverse=True)
    return all_items[:limit]


def _to_item(post: dict[str, Any], subreddit: str) -> Item:
    permalink = post.get("permalink") or ""
    full_url = f"https://www.reddit.com{permalink}" if permalink else (post.get("url") or "")
    return Item(
        source="reddit",
        id=str(post.get("id") or ""),
        title=str(post.get("title") or "(untitled)"),
        url=full_url,
        metric=int(post.get("ups") or post.get("score") or 0),
        metric_label="score",
        author=f"r/{subreddit}",
        extra={
            "subreddit": subreddit,
            "num_comments": post.get("num_comments", 0),
            "external_url": post.get("url"),
        },
    )
