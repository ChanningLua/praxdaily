"""B 站热门 scraper via the public web-interface API.

Uses the same endpoint the bilibili.com homepage's "热门" tab calls —
no login cookie needed, no rate-limit problems we've seen so far. If
B 站 ever locks it behind auth we'll switch to RSSHub or fall back to
the popular-recommendations endpoint.
"""

from __future__ import annotations

import httpx

from .types import Item


_API = "https://api.bilibili.com/x/web-interface/popular"
# Polite UA — B 站 used to 412 plain `python-httpx` agents on this endpoint.
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Referer": "https://www.bilibili.com/",
}


def scrape(limit: int = 20, *, timeout: float = 10.0) -> list[Item]:
    """Fetch top ``limit`` B 站 popular videos."""
    with httpx.Client(timeout=timeout, headers=_HEADERS) as c:
        # B 站's `ps` (page size) caps at 20 per page; if user wants more
        # we'd page, but for daily-digest 20 is plenty.
        ps = min(max(limit, 1), 20)
        r = c.get(_API, params={"ps": ps, "pn": 1})
        r.raise_for_status()
        body = r.json() or {}

    if body.get("code") != 0:
        # B 站 wraps errors in {code, message}; surface for diagnosis upstream.
        raise RuntimeError(f"bilibili api error: code={body.get('code')} message={body.get('message')}")

    raw = (body.get("data") or {}).get("list") or []
    return [_to_item(v) for v in raw[:limit]]


def _to_item(v: dict) -> Item:
    bvid = str(v.get("bvid") or "")
    stat = v.get("stat") or {}
    owner = v.get("owner") or {}
    return Item(
        source="bilibili",
        id=bvid,
        title=str(v.get("title") or "(untitled)"),
        url=f"https://www.bilibili.com/video/{bvid}" if bvid else str(v.get("short_link_v2") or ""),
        metric=int(stat.get("view") or 0),
        metric_label="view",
        author=str(owner.get("name") or ""),
        extra={
            "danmaku": stat.get("danmaku", 0),
            "like": stat.get("like", 0),
            "duration": v.get("duration"),
            "desc": (v.get("desc") or "")[:300],
            "rcmd_reason": (v.get("rcmd_reason") or {}).get("content", ""),
        },
    )
