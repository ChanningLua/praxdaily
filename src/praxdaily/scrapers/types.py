"""Common type for items returned by every scraper."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Item:
    """One scraped item, normalized across sources.

    Kept narrow on purpose: title + url + a numeric heat metric is what
    every consumer needs (filtering by keywords, sorting by hotness,
    rendering in the digest). Source-specific extras go in ``extra``.
    """

    source: str            # e.g. "hackernews", "bilibili"
    id: str                # platform-native id (string for json safety)
    title: str
    url: str
    metric: int = 0        # platform's hotness signal (HN score, B站 view count, ...)
    metric_label: str = ""  # human label, e.g. "score" / "play_count"
    author: str = ""
    extra: dict[str, Any] = field(default_factory=dict)
