"""Channels CRUD + test-send.

Reads/writes ``<cwd>/.prax/notify.yaml`` directly so the file format
stays the contract the prax CLI already expects (see
``prax/core/config_files.py::load_notify_config`` and
``prax/tools/notify.py::build_provider``).

The 0.2.0 milestone supports adding any of the four built-in providers
(``wechat_personal``, ``wechat_work_webhook``, ``feishu_webhook``,
``lark_webhook``); SMTP lives behind more knobs and lands later.

Test send goes through prax's own ``build_provider`` so we don't drift
from the CLI behaviour — what passes here passes for ``prax cron run``
too.
"""

from __future__ import annotations

from typing import Any

import yaml
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field


router = APIRouter(prefix="/api/channels", tags=["channels"])


SUPPORTED_PROVIDERS = {
    "wechat_personal",
    "wechat_work_webhook",
    "feishu_webhook",
    "lark_webhook",
}


def _notify_yaml_path(cwd) -> "Path":  # type: ignore[name-defined]
    from pathlib import Path

    return Path(cwd) / ".prax" / "notify.yaml"


def _load_channels(cwd) -> dict[str, dict[str, Any]]:
    path = _notify_yaml_path(cwd)
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"failed to parse {path}: {exc}",
        )
    channels = data.get("channels") if isinstance(data, dict) else None
    return channels if isinstance(channels, dict) else {}


def _save_channels(cwd, channels: dict[str, dict[str, Any]]) -> None:
    from pathlib import Path

    path = _notify_yaml_path(cwd)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"channels": channels}
    path.write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


# ── pydantic models ─────────────────────────────────────────────────────────


class ChannelUpsert(BaseModel):
    """Loose-typed payload — provider-specific fields validate at build time."""

    provider: str = Field(..., description="One of the SUPPORTED_PROVIDERS")
    # Provider-specific fields (any subset):
    url: str | None = None
    account_id: str | None = None
    to: str | None = None
    default_title_prefix: str | None = None

    def to_yaml_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"provider": self.provider}
        if self.url is not None:
            out["url"] = self.url
        if self.account_id is not None:
            out["account_id"] = self.account_id
        if self.to is not None:
            out["to"] = self.to
        if self.default_title_prefix is not None:
            out["default_title_prefix"] = self.default_title_prefix
        return out


class TestSendPayload(BaseModel):
    title: str = "praxdaily test"
    body: str = "如果你看到这条消息，praxdaily 配置生效了 ✅"
    level: str = "info"


# ── endpoints ───────────────────────────────────────────────────────────────


@router.get("")
async def list_channels(request: Request) -> JSONResponse:
    cwd = request.app.state.cwd
    channels = _load_channels(cwd)
    return JSONResponse(
        {
            "channels": [
                {"name": name, **cfg} for name, cfg in channels.items()
            ]
        }
    )


@router.put("/{name}")
async def upsert_channel(
    name: str, payload: ChannelUpsert, request: Request
) -> JSONResponse:
    if not name or "/" in name or "\\" in name:
        raise HTTPException(status_code=400, detail="invalid channel name")
    if payload.provider not in SUPPORTED_PROVIDERS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"unsupported provider {payload.provider!r}. "
                f"supported: {sorted(SUPPORTED_PROVIDERS)}"
            ),
        )

    cwd = request.app.state.cwd
    channels = _load_channels(cwd)
    channels[name] = payload.to_yaml_dict()
    _save_channels(cwd, channels)
    return JSONResponse({"name": name, **channels[name]})


@router.delete("/{name}")
async def delete_channel(name: str, request: Request) -> JSONResponse:
    cwd = request.app.state.cwd
    channels = _load_channels(cwd)
    if name not in channels:
        raise HTTPException(status_code=404, detail=f"channel {name!r} not found")
    del channels[name]
    _save_channels(cwd, channels)
    return JSONResponse({"deleted": name})


@router.post("/{name}/test")
async def test_channel(
    name: str, payload: TestSendPayload, request: Request
) -> JSONResponse:
    """Send a test notification through the named channel.

    Goes via ``prax.tools.notify.build_provider`` — same code path the
    cron dispatcher uses, so any failure here also predicts cron-time
    failure.
    """
    cwd = request.app.state.cwd
    channels = _load_channels(cwd)
    if name not in channels:
        raise HTTPException(status_code=404, detail=f"channel {name!r} not found")

    try:
        from prax.tools.notify import build_provider
    except ImportError as exc:
        raise HTTPException(
            status_code=503,
            detail=(
                "praxagent not importable — install with `npm install -g praxagent`. "
                f"({exc})"
            ),
        )

    try:
        provider = build_provider(channels[name])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    try:
        await provider.send(title=payload.title, body=payload.body, level=payload.level)
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"{type(exc).__name__}: {exc}",
        )

    return JSONResponse({"sent": True, "channel": name})
