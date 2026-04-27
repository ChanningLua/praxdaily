"""Daily-digest pipeline — pure-Python alternative to the prax skill path.

End-to-end:

  1. Load sources + keywords from `.prax/sources.yaml` (via the same
     loader the GUI uses, so what the user sees in the Sources tab is
     what runs here)
  2. For each enabled source whose ID is in ``SCRAPERS``, fetch up to
     ``limit`` items
  3. Filter by ``keywords.include`` / ``keywords.exclude``, keep top
     ``top_n`` per source by metric
  4. Render a markdown digest
  5. Push via the configured notify channel

The whole thing is sync (the scrapers are I/O-bound but each takes
<2s; not worth coroutine plumbing here). Failures per-source are
captured into the result so the GUI can surface "✗ twitter (no
scraper yet)" without halting the whole run — same tolerance the
SKILL.md describes.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .scrapers import SCRAPERS, Item


logger = logging.getLogger(__name__)


@dataclass
class SourceResult:
    id: str
    enabled: bool
    fetched: int = 0
    kept: int = 0      # after keyword filter + top_n cap
    error: str = ""    # populated on per-source failure


@dataclass
class PipelineResult:
    started_at: str
    finished_at: str = ""
    sources: list[SourceResult] = field(default_factory=list)
    digest_path: str = ""
    digest_chars: int = 0
    notify: dict[str, Any] = field(default_factory=dict)
    fatal_error: str = ""

    def to_dict(self) -> dict:
        return {
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "sources": [asdict(s) for s in self.sources],
            "digest_path": self.digest_path,
            "digest_chars": self.digest_chars,
            "notify": self.notify,
            "fatal_error": self.fatal_error,
        }


import re as _re


def _matches_keywords(text: str, *, include: list[str], exclude: list[str]) -> bool:
    """Case-insensitive include/exclude filter.

    Short ASCII tokens (anything that's all ASCII) must match at word
    boundaries — otherwise "AGI" matches "MAGIC", "AI" matches "PAID",
    etc. Chinese keywords substring-match because Chinese has no
    spaces. Mixed-script keywords go through substring too — they're
    rare and usually distinctive enough.

    No include keywords → everything matches (we don't want a missing
    list to silently drop everything).
    """
    if not text:
        return False
    t = text.lower()

    def _hits(keyword: str) -> bool:
        if not keyword:
            return False
        kw = keyword.lower()
        if kw.isascii():
            return _re.search(r"\b" + _re.escape(kw) + r"\b", t) is not None
        return kw in t

    if any(_hits(k) for k in exclude):
        return False
    if not include:
        return True
    return any(_hits(k) for k in include)


_SOURCE_META = {
    "hackernews": {"emoji": "📰", "label": "HackerNews"},
    "bilibili":   {"emoji": "📺", "label": "B 站热门"},
    "twitter":    {"emoji": "🐦", "label": "X / Twitter"},
    "zhihu":      {"emoji": "💡", "label": "知乎"},
}

# How to render each source's raw `metric` into something a Chinese
# reader scans easily. Keep the metric_label fallback for unknowns.
_METRIC_FORMATTERS = {
    "score": lambda n: f"🔥 {n} 分",
    "view":  lambda n: f"👁 {_human_count(n)} 播放",
}


def _human_count(n: int) -> str:
    """1234567 → '123.5万' for Chinese readability. Falls back to comma
    style for sub-万 numbers and English-context labels."""
    if n >= 100_000_000:
        return f"{n / 100_000_000:.1f}亿"
    if n >= 10_000:
        return f"{n / 10_000:.1f}万"
    return f"{n:,}"


def _format_metric(metric_label: str, metric: int) -> str:
    if metric <= 0:
        return ""
    fn = _METRIC_FORMATTERS.get(metric_label)
    return fn(metric) if fn else f"{metric_label} {metric:,}"


def _render_markdown(date: str, by_source: dict[str, list[Item]]) -> str:
    """Render the full daily digest as a single string. Used for the
    on-disk archive (`daily-digest.md`); wechat sends are chunked
    separately by ``_render_chunks``.
    """
    chunks = _render_chunks(date, by_source)
    return "\n\n".join(chunks) + "\n"


# Wechat (personal) silently truncates single text messages somewhere
# around 2000 Chinese chars. Be conservative — a few items always look
# better than a long message getting cut mid-sentence.
WECHAT_CHUNK_BUDGET = 1500


def _render_chunks(date: str, by_source: dict[str, list[Item]]) -> list[str]:
    """Render the digest as a LIST of strings, each safely under
    ``WECHAT_CHUNK_BUDGET`` chars.

    Layout per chunk:
    - First chunk = header + first source (or empty-state notice)
    - One source per chunk by default
    - If a single source overflows the budget, split it further by
      pivoting to a fresh chunk after the last item that fits.
    - Last chunk gets the footer.

    Why one source per chunk: it keeps each wechat message coherent
    ("Here's HN today"), much friendlier than arbitrary mid-section
    splits. The footer message gives the user a clear "end of report"
    signal.
    """
    total = sum(len(v) for v in by_source.values())
    if total == 0:
        return [
            f"📅 AI 日报 · {date}\n\n今天各源在筛选词下都没有命中。\n换组关键词或扩大抓取量再试。"
        ]

    chunks: list[str] = []
    sections = list(by_source.items())

    # Header chunk — title + table of contents (which sources, how many)
    toc_lines = [f"📅 AI 日报 · {date}", "", f"今日 {total} 条："]
    for sid, items in sections:
        if not items:
            continue
        meta = _SOURCE_META.get(sid, {"emoji": "🔗", "label": sid})
        toc_lines.append(f"  {meta['emoji']} {meta['label']} · {len(items)} 条")
    toc_lines.append("")
    toc_lines.append("👇 详细内容看下面几条")
    chunks.append("\n".join(toc_lines))

    # One section per chunk, splitting further if a section is too long.
    for sid, items in sections:
        if not items:
            continue
        meta = _SOURCE_META.get(sid, {"emoji": "🔗", "label": sid})
        section_chunks = _split_section_by_budget(meta, items)
        chunks.extend(section_chunks)

    # Footer
    chunks.append(f"——— 共 {total} 条 · praxdaily 自动生成 ———")
    return chunks


def _split_section_by_budget(meta: dict, items: list[Item]) -> list[str]:
    """Pack as many items into a chunk as fit under the budget; spill
    overflow into a continuation chunk with a `(续)` header so the
    reader still knows it's the same source."""
    budget = WECHAT_CHUNK_BUDGET
    out_chunks: list[str] = []
    cur_lines: list[str] = []
    cur_chars = 0
    cur_count = 0
    part_num = 0

    def _flush(continuation: bool):
        nonlocal cur_lines, cur_chars, cur_count, part_num
        if not cur_lines:
            return
        part_num += 1
        suffix = f"  (续 {part_num})" if continuation else ""
        header = f"{meta['emoji']} {meta['label']}{suffix}\n———————\n"
        out_chunks.append(header + "\n".join(cur_lines))
        cur_lines = []
        cur_chars = 0
        cur_count = 0

    for i, it in enumerate(items, 1):
        block_lines = [f"{i}. {it.title}"]
        sub_parts: list[str] = []
        if it.author:
            sub_parts.append(f"by {it.author}")
        m = _format_metric(it.metric_label, it.metric)
        if m:
            sub_parts.append(m)
        if sub_parts:
            block_lines.append("   " + " · ".join(sub_parts))
        block_lines.append(f"   🔗 {it.url}")
        block = "\n".join(block_lines) + "\n"

        if cur_chars + len(block) > budget and cur_lines:
            _flush(continuation=part_num >= 1)
        cur_lines.append(block)
        cur_chars += len(block)
        cur_count += 1

    _flush(continuation=part_num >= 1)
    return out_chunks


def _resolve_channel(cwd) -> tuple[str, dict] | None:
    """Find the channel to push to.

    Strategy: prefer the channel name on the cron job named "行业最近进展"
    or any cron job with a notify_channel; else first wechat_personal in
    notify.yaml; else first channel; else None.
    """
    import yaml

    cwd = Path(cwd)
    notify_path = cwd / ".prax" / "notify.yaml"
    if not notify_path.exists():
        return None
    try:
        notify = yaml.safe_load(notify_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return None
    channels = notify.get("channels") or {}
    if not channels:
        return None

    # 1. cron job hint
    cron_path = cwd / ".prax" / "cron.yaml"
    if cron_path.exists():
        try:
            cron = yaml.safe_load(cron_path.read_text(encoding="utf-8")) or {}
            for job in (cron.get("jobs") or []):
                ch = (job.get("notify_channel") or "").strip()
                if ch and ch in channels:
                    return ch, channels[ch]
        except yaml.YAMLError:
            pass

    # 2. first wechat_personal
    for name, cfg in channels.items():
        if (cfg or {}).get("provider") == "wechat_personal":
            return name, cfg

    # 3. first channel
    name = next(iter(channels))
    return name, channels[name]


async def run(*, cwd) -> PipelineResult:
    """Async orchestrator (notify provider is async). Returns a
    PipelineResult; never raises on per-source failures (those are
    captured into ``result.sources``). Hard infrastructure failures
    (no notify channel, write failure) go into ``result.fatal_error``.
    """
    from .routes.sources import _load as load_sources_config  # reuse single source of truth

    started = datetime.now()
    result = PipelineResult(started_at=started.isoformat(timespec="seconds"))

    try:
        config = load_sources_config(cwd)
    except Exception as exc:  # noqa: BLE001
        result.fatal_error = f"failed to load sources.yaml: {exc}"
        result.finished_at = datetime.now().isoformat(timespec="seconds")
        return result

    keywords = config.get("keywords") or {}
    include = list(keywords.get("include") or [])
    exclude = list(keywords.get("exclude") or [])

    by_source: dict[str, list[Item]] = {}
    for src in (config.get("sources") or []):
        sid = src.get("id")
        sr = SourceResult(id=sid, enabled=bool(src.get("enabled")))
        result.sources.append(sr)
        if not sr.enabled:
            continue
        scraper = SCRAPERS.get(sid)
        if scraper is None:
            sr.error = "no native scraper (skipped)"
            continue
        limit = int(src.get("limit") or 20)
        top_n = int(src.get("top_n") or 5)
        min_metric = int(src.get("min_metric") or 0)
        try:
            items = scraper(limit=limit)
        except Exception as exc:  # noqa: BLE001
            sr.error = f"{type(exc).__name__}: {exc}"
            continue
        sr.fetched = len(items)
        kept = [
            it for it in items
            if it.metric >= min_metric
            and _matches_keywords(f"{it.title} {it.extra.get('desc', '')}", include=include, exclude=exclude)
        ]
        kept.sort(key=lambda i: i.metric, reverse=True)
        kept = kept[:top_n]
        sr.kept = len(kept)
        if kept:
            by_source[sid] = kept

    # Render + persist
    date = started.strftime("%Y-%m-%d")
    md = _render_markdown(date, by_source)
    chunks = _render_chunks(date, by_source)
    vault = Path(cwd) / ".prax" / "vault" / date
    vault.mkdir(parents=True, exist_ok=True)
    digest_path = vault / "daily-digest.md"
    digest_path.write_text(md, encoding="utf-8")
    result.digest_path = str(digest_path)
    result.digest_chars = len(md)

    # Push via configured notify channel
    resolved = _resolve_channel(cwd)
    if not resolved:
        result.notify = {"sent": False, "error": "no notify channel resolvable from .prax/notify.yaml"}
    else:
        ch_name, ch_cfg = resolved
        result.notify = await _push_chunks(ch_name, ch_cfg, chunks=chunks)

    result.finished_at = datetime.now().isoformat(timespec="seconds")
    return result


def _classify_send_error(exc: Exception, channel_cfg: dict) -> dict[str, Any]:
    """Translate raw provider exceptions into actionable hint dicts.

    The GUI renders ``hint_kind`` specially (e.g. with a "怎么修" button).
    Plain string matching on ``str(exc)`` is fine here — these tokens come
    from prax's own error formatter and are stable.
    """
    msg = str(exc)
    is_wechat = (channel_cfg or {}).get("provider") == "wechat_personal"
    if is_wechat and ("ret=-2" in msg or "会话上下文" in msg):
        account_id = (channel_cfg or {}).get("account_id", "")
        return {
            "hint_kind": "wechat_session_lost",
            "hint_title": "iLink bot 会话上下文掉了 (ret=-2)",
            "hint_steps": [
                "打开微信，找到 bot 联系人",
                f"给它发任意一句话（比如 ping）{('— 账号 ' + account_id) if account_id else ''}",
                "回 praxdaily 重新点「立即触发」即可",
            ],
        }
    if is_wechat and "account" in msg.lower() and "not found" in msg.lower():
        return {
            "hint_kind": "wechat_account_missing",
            "hint_title": "微信账号没登录",
            "hint_steps": [
                "切到「微信账号」屏",
                "点「+ 登录新账号」用微信扫码登录",
                "回到这里再点「立即触发」",
            ],
        }
    return {}


async def _push_chunks(channel_name: str, channel_cfg: dict, *, chunks: list[str]) -> dict:
    """Send the digest as multiple chat messages.

    Why chunked: personal WeChat truncates single text payloads around
    2000 Chinese chars; users were seeing the tail of the digest dropped.
    Sending one message per source preserves all content + reads more
    naturally in chat (each section as its own "post").

    A small inter-message delay keeps us under iLink rate limits and
    avoids sub-second messages getting reordered on the recipient side.
    """
    try:
        from prax.tools.notify import build_provider  # type: ignore
    except ImportError as exc:
        return {"sent": False, "channel": channel_name, "error": f"praxagent not importable: {exc}"}

    try:
        provider = build_provider(channel_cfg)
    except ValueError as exc:
        payload = {"sent": False, "channel": channel_name, "error": str(exc)}
        payload.update(_classify_send_error(exc, channel_cfg))
        return payload

    sent_count = 0
    sent_chars = 0
    for i, chunk in enumerate(chunks):
        if i > 0:
            await asyncio.sleep(2.0)  # iLink dislikes rapid same-recipient sends
        # iLink occasionally returns ret=-2 ("session context lost") between
        # chunks even when the previous one succeeded. Retry with a back-off
        # before giving up — this is a known transport flake, not user error.
        last_exc: Exception | None = None
        for attempt in range(3):
            if attempt > 0:
                await asyncio.sleep(2.5 * attempt)
            try:
                await provider.send(title="", body=chunk, level="info")
                last_exc = None
                break
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
        if last_exc is not None:
            payload: dict[str, Any] = {
                "sent": False, "channel": channel_name,
                "error": f"chunk {i+1}/{len(chunks)} failed after retries: {type(last_exc).__name__}: {last_exc}",
                "chunks_sent": sent_count, "chunks_total": len(chunks),
            }
            payload.update(_classify_send_error(last_exc, channel_cfg))
            return payload
        sent_count += 1
        sent_chars += len(chunk)

    return {
        "sent": True, "channel": channel_name,
        "chunks_sent": sent_count, "chunks_total": len(chunks),
        "chars": sent_chars,
    }
