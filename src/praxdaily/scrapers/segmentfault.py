"""SegmentFault scraper — Atom feed parsing.

API: ``https://segmentfault.com/feeds`` 是公开的最新问答 Atom feed，
无需认证、稳定。

SF 的内容结构是 Q&A，比 HN/掘金更偏"求助/讨论"，downstream keyword
filter（AI/LLM/GPT 等）能从一堆通用问答里挑出 AI 相关的。

Feed 不带浏览/赞数，但**最新发布的问题** = 当下用户最关心的问题。
metric 用倒序 position（越靠前越新越热），让 ``top_n`` 取最新 N 条。
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET

import httpx

from .types import Item


logger = logging.getLogger(__name__)


_FEED_URL = "https://segmentfault.com/feeds"
_USER_AGENT = "Mozilla/5.0 (praxdaily-scraper/0.7)"
_ATOM_NS = {"a": "http://www.w3.org/2005/Atom"}
_MAX_METRIC = 1000


def scrape(limit: int = 20, *, timeout: float = 10.0) -> list[Item]:
    """Fetch up to ``limit`` recent questions from SegmentFault."""
    headers = {"User-Agent": _USER_AGENT}
    with httpx.Client(timeout=timeout, headers=headers) as c:
        try:
            r = c.get(_FEED_URL)
            r.raise_for_status()
            xml_text = r.text
        except Exception as exc:  # noqa: BLE001
            logger.warning("segmentfault: fetch failed: %s", exc)
            return []

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        logger.warning("segmentfault: feed parse failed: %s", exc)
        return []

    entries = root.findall("a:entry", _ATOM_NS)[:limit]
    items: list[Item] = []
    for idx, entry in enumerate(entries):
        item = _entry_to_item(entry, position=idx, total=len(entries))
        if item is not None:
            items.append(item)
    return items


def _entry_to_item(entry: ET.Element, *, position: int, total: int) -> Item | None:
    title_el = entry.find("a:title", _ATOM_NS)
    link_el = entry.find("a:link", _ATOM_NS)
    id_el = entry.find("a:id", _ATOM_NS)
    author_el = entry.find("a:author/a:name", _ATOM_NS)
    summary_el = entry.find("a:summary", _ATOM_NS)
    if title_el is None or link_el is None:
        return None

    title = (title_el.text or "").strip()
    url = link_el.attrib.get("href", "")
    eid = (id_el.text or "").strip() if id_el is not None else url

    # SF entry id is often a URL; extract trailing question id when present.
    short_id = eid
    m = re.search(r"/q/(\d+)", eid)
    if m:
        short_id = m.group(1)

    return Item(
        source="segmentfault",
        id=short_id,
        title=title or "(untitled)",
        url=url,
        # Position-as-metric: feed is newest-first; top of feed = top of metric.
        metric=max(_MAX_METRIC - position, 1),
        metric_label="recency",
        author=(author_el.text or "").strip() if author_el is not None else "",
        extra={
            "summary": (summary_el.text or "").strip() if summary_el is not None else "",
            "position": position,
        },
    )
