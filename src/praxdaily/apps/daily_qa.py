"""Daily-digest Q&A — the conversational half of the daily news app.

Subscribes to bridge inbound events and handles a small command set:

  ``今天`` / ``today``           — re-send today's digest
  ``昨天`` / ``yesterday``       — send yesterday's digest
  ``查 <keyword>`` / ``search``  — search recent N days for matches
  ``退订`` / ``unsubscribe``     — mark binding expired, stop pushes
  ``帮助`` / ``help`` / ``?``    — print available commands
  (anything else)               — friendly fallback + help hint

Commands match leading whitespace + Chinese/English forms case-insensitively.

Implemented as a free-standing function ``handle`` that the bridge
inbound webhook (real or simulated) can invoke after the binding is
resolved. The function is async because outbound replies hit the
network — but it has no other I/O dependencies so it's straightforward
to unit test.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .. import bridge as bridge_pkg


logger = logging.getLogger(__name__)


# Commands that re-send a specific day's digest.
TODAY_TOKENS = {"今天", "today", "todays digest", "今日"}
YESTERDAY_TOKENS = {"昨天", "yesterday"}

# Keyword-search prefixes — all map to the same handler.
SEARCH_PREFIXES = ("查 ", "查询 ", "search ")

UNSUB_TOKENS = {"退订", "取消订阅", "unsub", "unsubscribe", "stop"}

HELP_TOKENS = {"?", "？", "帮助", "help", "menu", "命令"}

# Keyword search scope.
SEARCH_DAYS = 7
SEARCH_MAX_RESULTS = 5

HELP_TEXT = (
    "📋 可用命令\n"
    "  今天 / today        — 重发当天日报\n"
    "  昨天 / yesterday    — 发昨天日报\n"
    "  查 <关键词>         — 在最近 7 天日报里搜\n"
    "                        例：查 OpenAI\n"
    "  退订               — 停止接收日报\n"
    "  帮助 / ?           — 显示这条说明"
)


# ── command parsing ────────────────────────────────────────────────────────


def _norm(text: str) -> str:
    return (text or "").strip().lower()


def parse_command(content: str) -> tuple[str, str]:
    """Classify an inbound text into (command_kind, argument).

    ``command_kind`` is one of:
      ``today`` ``yesterday`` ``search`` ``unsubscribe`` ``help`` ``other``
    """
    n = _norm(content)
    if not n:
        return "other", ""

    if n in TODAY_TOKENS:
        return "today", ""
    if n in YESTERDAY_TOKENS:
        return "yesterday", ""
    for p in SEARCH_PREFIXES:
        if n.startswith(p):
            kw = content.strip()[len(p):].strip()
            return "search", kw
    if n in UNSUB_TOKENS:
        return "unsubscribe", ""
    if n in HELP_TOKENS:
        return "help", ""
    return "other", content


# ── digest lookup ──────────────────────────────────────────────────────────


def _vault_dir(cwd) -> Path:
    return Path(str(cwd)) / ".prax" / "vault"


def find_digest(cwd, *, date: str) -> str | None:
    """Return the digest text for ``date`` (YYYY-MM-DD), or None if not on disk."""
    p = _vault_dir(cwd) / date / "daily-digest.md"
    if not p.exists():
        return None
    try:
        return p.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("daily_qa: failed to read %s: %s", p, exc)
        return None


def search_recent(cwd, *, keyword: str, days: int = SEARCH_DAYS) -> list[dict[str, Any]]:
    """Plain-text search across the last ``days`` digests.

    Returns up to ``SEARCH_MAX_RESULTS`` hits, newest first. Each hit:

      {"date": "YYYY-MM-DD", "title": str, "url": str, "context": str}

    Match rules:
      - Case-insensitive substring match on each titled bullet (lines
        starting with ``\\d+\\. ``).
      - Empty / whitespace keyword returns no hits.
    """
    keyword = (keyword or "").strip()
    if not keyword:
        return []
    needle = keyword.lower()

    today = datetime.now().date()
    hits: list[dict[str, Any]] = []
    for delta in range(days):
        d = (today - timedelta(days=delta)).isoformat()
        text = find_digest(cwd, date=d)
        if not text:
            continue
        # Walk the digest looking for "1. <title>" entries; capture the
        # following indented author/metric line and the URL line.
        lines = text.splitlines()
        i = 0
        while i < len(lines):
            m = re.match(r"^\s*\d+\.\s+(.*?)\s*$", lines[i])
            if not m:
                i += 1
                continue
            title = m.group(1)
            # Look ahead for url line within the next 4 lines.
            url = ""
            ctx_extra = ""
            for j in range(i + 1, min(i + 5, len(lines))):
                ll = lines[j]
                u = re.search(r"https?://\S+", ll)
                if u and not url:
                    url = u.group(0).rstrip("】)）]，,。.")
                if "·" in ll and not ctx_extra:
                    ctx_extra = ll.strip()
            if needle in title.lower() or needle in (ctx_extra or "").lower():
                hits.append({
                    "date": d,
                    "title": title,
                    "url": url,
                    "context": ctx_extra,
                })
                if len(hits) >= SEARCH_MAX_RESULTS:
                    return hits
            i += 1
    return hits


def render_search_results(keyword: str, hits: list[dict[str, Any]]) -> str:
    if not hits:
        return f"近 {SEARCH_DAYS} 天日报里没找到「{keyword}」相关内容。"
    lines = [f"🔎 关于「{keyword}」近 {SEARCH_DAYS} 天命中 {len(hits)} 条："]
    for h in hits:
        line = f"\n📅 {h['date']}\n  {h['title']}"
        if h.get("context"):
            line += f"\n  {h['context']}"
        if h.get("url"):
            line += f"\n  🔗 {h['url']}"
        lines.append(line)
    return "\n".join(lines)


# ── outbound to user (via bridge) ──────────────────────────────────────────


async def _reply_to_user(
    conn: sqlite3.Connection,
    *,
    app_user_id: str,
    content: str,
) -> bool:
    """Send a reply through whichever active binding the user has.

    Uses bridge.broadcast's per-binding sender directly — that lets us
    reuse the retry / logging / message-table-recording behavior, but
    targeted at one user instead of broadcasting.
    """
    bindings = bridge_pkg.bindings.list_for_user(conn, app_user_id)
    if not bindings:
        logger.warning("daily_qa: no active binding for %s", app_user_id)
        return False
    b = bindings[0]
    ok, err = await bridge_pkg.broadcast._send_one_chunk(binding=b, body=content)
    if ok:
        bridge_pkg.broadcast._record_message(
            conn, binding=b, content=content, status="sent",
        )
    else:
        bridge_pkg.broadcast._record_message(
            conn, binding=b, content=content, status="failed", error=err,
        )
        logger.warning("daily_qa: send to %s failed: %s", app_user_id, err)
    return ok


# ── dispatcher ─────────────────────────────────────────────────────────────


async def handle(
    conn: sqlite3.Connection,
    *,
    cwd,
    app_user_id: str,
    content: str,
) -> dict[str, Any]:
    """Handle one inbound user message.

    Returns a dict ``{command, action, reply_sent}`` describing what we did.
    Always sends a reply (even on "other" we send the help hint), unless
    the user has no active binding (then we just log).
    """
    cmd, arg = parse_command(content)
    logger.info(
        "daily_qa: app_user_id=%s cmd=%s arg=%r content=%r",
        app_user_id, cmd, arg, content[:80],
    )

    if cmd == "today":
        date = datetime.now().date().isoformat()
        digest = find_digest(cwd, date=date)
        reply = digest if digest else f"今天（{date}）还没生成日报。"
    elif cmd == "yesterday":
        date = (datetime.now().date() - timedelta(days=1)).isoformat()
        digest = find_digest(cwd, date=date)
        reply = digest if digest else f"昨天（{date}）的日报没找到。"
    elif cmd == "search":
        hits = search_recent(cwd, keyword=arg)
        reply = render_search_results(arg, hits)
    elif cmd == "unsubscribe":
        # Send the confirmation BEFORE expiring — once the binding is
        # gone we have no way to push the goodbye message.
        reply = "✅ 已退订，下一期日报不再发送。想恢复，再扫一次绑定二维码即可。"
        sent = await _reply_to_user(conn, app_user_id=app_user_id, content=reply)
        for b in bridge_pkg.bindings.list_for_user(conn, app_user_id):
            bridge_pkg.bindings.expire(conn, b.binding_id)
        return {"command": cmd, "argument": arg, "reply_sent": sent}
    elif cmd == "help":
        reply = HELP_TEXT
    else:
        reply = f"我没看懂「{content[:30]}」。\n{HELP_TEXT}"

    sent = await _reply_to_user(conn, app_user_id=app_user_id, content=reply)
    return {"command": cmd, "argument": arg, "reply_sent": sent}
