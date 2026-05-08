"""Outbound content moderation — sensitive-word filter.

Light-touch filter that blocks obviously risky content before it hits
iLink. iLink's risk-control system penalizes accounts that send
sensitive content; one bad message can shorten the bot's lifespan from
months to days. This is the cheapest insurance.

Sources:
  - Built-in default list: a small core set covering the categories
    that most reliably trigger 微信 risk control (政治敏感、违法、色情).
    Intentionally minimal — over-filtering is worse than under-filtering
    for a daily news bot whose content is low-risk by topic anyway.
  - User overrides: ``<cwd>/.prax/sensitive_words.yaml`` if present
    can extend or replace the default list:

      ```yaml
      mode: extend            # or "replace"
      words:
        - 关键词1
        - 关键词2
      ```

API surface is intentionally tiny:
  - ``check(text) -> tuple[bool, str]`` — returns (is_safe, hit_word)
  - ``load_wordlist(cwd)`` — reload from yaml; called lazily, cached

Match is case-insensitive substring. We deliberately avoid regex /
lookbehind / fancy NLP — keeps the filter predictable, fast, and
auditable.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path
from typing import Iterable


logger = logging.getLogger(__name__)


# Minimal built-in seed list. These are categories with the highest
# correlation to iLink account suspension based on community reports.
# Intentionally short — most daily-news content won't match any of them.
DEFAULT_WORDS: tuple[str, ...] = (
    # 政治敏感（最常导致封号的类别）
    "习近平", "毛泽东", "邓小平", "六四", "天安门事件", "法轮功",
    # 违法 / 暴力
    "枪支买卖", "毒品交易", "代孕",
    # 金融诈骗
    "洗钱", "兼职刷单",
    # 色情
    "成人电影", "色情网站",
)


def _wordlist_path(cwd) -> Path:
    return Path(str(cwd)) / ".prax" / "sensitive_words.yaml"


@lru_cache(maxsize=4)
def _cached_wordlist(cwd_str: str) -> tuple[str, ...]:
    """Cached on cwd path. Cleared by ``load_wordlist(cwd, reload=True)``."""
    path = _wordlist_path(cwd_str)
    if not path.exists():
        return DEFAULT_WORDS
    try:
        import yaml  # type: ignore
    except ImportError:
        logger.warning("moderation: PyYAML not available, using default wordlist")
        return DEFAULT_WORDS
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        logger.warning("moderation: failed to parse %s: %s — using default", path, exc)
        return DEFAULT_WORDS
    user_words = tuple(str(w) for w in (data.get("words") or []) if str(w).strip())
    mode = (data.get("mode") or "extend").lower()
    if mode == "replace":
        return user_words
    return DEFAULT_WORDS + user_words


def load_wordlist(cwd, *, reload: bool = False) -> tuple[str, ...]:
    """Return the active wordlist for ``cwd``. Pass ``reload=True`` to
    bust the cache after editing the yaml file."""
    if reload:
        _cached_wordlist.cache_clear()
    return _cached_wordlist(str(cwd))


def check(text: str, *, cwd) -> tuple[bool, str]:
    """Return ``(is_safe, hit_word)``. ``hit_word`` is empty when safe.

    Returns the FIRST hit only — no need to enumerate all matches; the
    caller just wants a yes/no decision plus a label for the alert
    payload.
    """
    if not text:
        return True, ""
    needle = text.lower()
    for w in load_wordlist(cwd):
        if w.lower() in needle:
            return False, w
    return True, ""
