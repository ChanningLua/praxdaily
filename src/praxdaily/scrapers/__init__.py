"""Native HTTP scrapers — no autocli, no Chrome extension, no LLM.

Each scraper is a pure function that returns a list of ``Item``s.
Plugging in a new source = drop a module here that exports
``scrape(limit) -> list[Item]`` and register it in ``SCRAPERS``.

The point of keeping this dead simple: the original ai-news-daily
pipeline let an LLM shell out to autocli to do scraping, which was
slow, flaky, and required a paid/private toolchain. Scraping is
deterministic — let it be deterministic.
"""

from __future__ import annotations

from .types import Item
from . import hn, bilibili


# Source ID → scrape callable. Keep keys aligned with .prax/sources.yaml
# so user-visible config doesn't drift from registration here.
SCRAPERS = {
    "hackernews": hn.scrape,
    "bilibili": bilibili.scrape,
}


__all__ = ["Item", "SCRAPERS"]
