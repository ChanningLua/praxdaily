"""Sources CRUD — read/write ``<cwd>/.prax/sources.yaml``.

The contract was added in praxagent 0.5.4: ``ai-news-daily``'s
``SKILL.md`` Step 1.5 loads this file (or its built-in DEFAULTS when
the file is missing). The praxdaily Sources screen is a thin yaml
editor on top of the same shape.

DEFAULTS deliberately mirror the values baked into the skill prompt
so a GET with no file on disk reflects what the skill will actually
do — the user sees the real defaults, not an empty form.
"""

from __future__ import annotations

from typing import Any

import yaml
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel


router = APIRouter(prefix="/api/sources", tags=["sources"])


# Mirrors the DEFAULTS embedded in
# src/prax/skills/ai-news-daily/SKILL.md Step 1.5 — keep in sync.
DEFAULT_SOURCES: list[dict[str, Any]] = [
    # Twitter/Zhihu need login state — no native scraper yet, kept here so
    # the sources.yaml shape doesn't churn when we add them later.
    {"id": "twitter",   "enabled": False, "limit": 50, "top_n": 5, "min_metric": 0},
    {"id": "zhihu",     "enabled": False, "limit": 30, "top_n": 5, "min_metric": 0},
    # B 站 "popular" API is dominated by gaming/anime; even strict AI
    # keyword filtering gives 0-2 hits per day. Off by default — opt in
    # if you want broad popular signal.
    {"id": "bilibili",  "enabled": False, "limit": 20, "top_n": 5, "min_metric": 100_000},
    # HN front page goes from ~1000 (top story) down to single digits.
    # top_n=5 = the 5 most-upvoted AI-related stories from today's
    # front page. min_metric=100 keeps the bar above "barely noticed".
    {"id": "hackernews","enabled": True,  "limit": 30, "top_n": 5, "min_metric": 100},
]
DEFAULT_KEYWORDS: dict[str, list[str]] = {
    # Precise AI-domain terms only. Loose words like "推理" / "agent" /
    # "智能体" match gaming-context content (推理小说 / 角色 agent /
    # 智能匹配 features), so they're left out.
    "include": [
        "AI", "AGI", "LLM", "GPT", "Claude", "Anthropic", "OpenAI",
        "大模型", "RAG", "微调", "transformer", "diffusion",
        "neural", "embedding", "vector",
    ],
    "exclude": [],
}

KNOWN_SOURCE_IDS = {"twitter", "zhihu", "bilibili", "hackernews"}


def _sources_yaml_path(cwd):
    from pathlib import Path

    return Path(cwd) / ".prax" / "sources.yaml"


def _load(cwd) -> dict[str, Any]:
    """Load .prax/sources.yaml, layering user values over DEFAULTS.

    Missing fields fall back to defaults so the GUI always has the
    full shape to render. This matches the SKILL.md's behavior.
    """
    path = _sources_yaml_path(cwd)
    user_data: dict[str, Any] = {}
    if path.exists():
        try:
            user_data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise HTTPException(
                status_code=500,
                detail=f"failed to parse {path}: {exc}",
            )

    # Layer sources by id; user values override default fields.
    user_sources = {
        s.get("id"): s for s in (user_data.get("sources") or []) if isinstance(s, dict) and s.get("id")
    }
    merged_sources: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for default in DEFAULT_SOURCES:
        sid = default["id"]
        seen_ids.add(sid)
        merged = dict(default)
        if sid in user_sources:
            merged.update({k: v for k, v in user_sources[sid].items() if k != "id"})
        merged_sources.append(merged)
    # Preserve any custom user sources we don't know about (forward-compat).
    for sid, src in user_sources.items():
        if sid not in seen_ids:
            merged_sources.append(src)

    keywords = dict(DEFAULT_KEYWORDS)
    user_kw = user_data.get("keywords") or {}
    if isinstance(user_kw, dict):
        if "include" in user_kw and isinstance(user_kw["include"], list):
            keywords["include"] = list(user_kw["include"])
        if "exclude" in user_kw and isinstance(user_kw["exclude"], list):
            keywords["exclude"] = list(user_kw["exclude"])

    return {
        "sources": merged_sources,
        "keywords": keywords,
        "is_user_config_present": path.exists(),
    }


class SourceItem(BaseModel):
    id: str
    enabled: bool = True
    limit: int = 20
    top_n: int = 10


class KeywordsModel(BaseModel):
    include: list[str] = []
    exclude: list[str] = []


class SourcesPayload(BaseModel):
    sources: list[SourceItem]
    keywords: KeywordsModel


@router.get("")
async def get_sources(request: Request) -> JSONResponse:
    return JSONResponse(_load(request.app.state.cwd))


@router.put("")
async def upsert_sources(payload: SourcesPayload, request: Request) -> JSONResponse:
    """Write the full sources + keywords config.

    No partial updates — the GUI always sends the complete shape, so
    anything the user removed in the UI gets removed on disk.
    """
    cwd = request.app.state.cwd
    seen_ids: set[str] = set()
    out_sources: list[dict[str, Any]] = []
    for src in payload.sources:
        if not src.id.strip():
            raise HTTPException(status_code=400, detail="source id must be non-empty")
        if src.id in seen_ids:
            raise HTTPException(status_code=400, detail=f"duplicate source id: {src.id!r}")
        seen_ids.add(src.id)
        if src.limit < 1 or src.limit > 1000:
            raise HTTPException(
                status_code=400,
                detail=f"source {src.id!r}: limit must be 1..1000",
            )
        if src.top_n < 1 or src.top_n > src.limit:
            raise HTTPException(
                status_code=400,
                detail=f"source {src.id!r}: top_n must be 1..{src.limit}",
            )
        out_sources.append(
            {
                "id": src.id,
                "enabled": src.enabled,
                "limit": src.limit,
                "top_n": src.top_n,
            }
        )

    out = {
        "sources": out_sources,
        "keywords": {
            "include": [s for s in payload.keywords.include if s.strip()],
            "exclude": [s for s in payload.keywords.exclude if s.strip()],
        },
    }

    path = _sources_yaml_path(cwd)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(out, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return JSONResponse(_load(cwd))


@router.delete("")
async def reset_sources(request: Request) -> JSONResponse:
    """Delete .prax/sources.yaml so the skill falls back to DEFAULTS."""
    path = _sources_yaml_path(request.app.state.cwd)
    if path.exists():
        path.unlink()
    return JSONResponse(_load(request.app.state.cwd))
