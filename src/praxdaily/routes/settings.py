"""Settings: per-workspace LLM credential + relay (base_url) management.

Two files are managed here:

- ``<cwd>/.prax/.env`` — API keys (workspace-scoped, chmod 600).
- ``~/.prax/models.yaml`` — base_url overrides per provider (user-scoped,
  field-level deep-merged with bundled defaults by prax's
  ``config_merge.merge_providers`` so we only need to write the fields
  we want to change).

We never return raw secret values to the browser — only "configured /
last 4 chars" — so opening this page on a shared screen doesn't leak
keys. base_url is non-secret and shown verbatim.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Any

import yaml
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field


router = APIRouter(prefix="/api/settings", tags=["settings"])


# Each entry:
#   provider: matches the key under `providers:` in models.yaml
#   default_base_url: shown as placeholder when no override set
KNOWN_KEYS = [
    {
        "name": "OPENAI_API_KEY",
        "label": "OpenAI / 兼容服务",
        "hint": "OpenAI 官方走 api.openai.com；走中转站就把『服务地址』换成中转的 base_url。",
        "provider": "openai",
        "default_base_url": "https://api.openai.com/v1",
    },
    {
        "name": "ZHIPU_API_KEY",
        "label": "智谱 GLM",
        "hint": "GLM-4-Flash 是免费档，新用户推荐先填这个跑通。",
        "provider": "zhipu",
        "default_base_url": "https://open.bigmodel.cn/api/paas/v4",
    },
    {
        "name": "ANTHROPIC_API_KEY",
        "label": "Anthropic Claude",
        "hint": "claude.ai 官方走 api.anthropic.com；走中转站（如 oneapi）把『服务地址』换成中转的 base_url。",
        "provider": "anthropic",
        "default_base_url": "https://api.anthropic.com",
    },
]

KNOWN_KEY_NAMES = {k["name"] for k in KNOWN_KEYS}
KNOWN_PROVIDERS = {k["provider"] for k in KNOWN_KEYS}


class SetEnvBody(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)
    value: str = Field(default="", max_length=4096)  # empty = delete


class SetProviderBody(BaseModel):
    provider: str = Field(..., min_length=1, max_length=32)
    base_url: str = Field(default="", max_length=512)  # empty = remove override


class ProbeBody(BaseModel):
    provider: str = Field(..., min_length=1, max_length=32)
    base_url: str = Field(..., min_length=1, max_length=512)


def _env_path(cwd) -> Path:
    return Path(os.fspath(cwd)) / ".prax" / ".env"


def _parse_env_file(path: Path) -> dict[str, str]:
    """Tiny .env parser: KEY=value per line, supports # comments + quoted values.

    Mirrors prax's own .env loader semantics (KEY=value, comments allowed),
    so what we read here is what prax sees at runtime.
    """
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$", line)
        if not m:
            continue
        k, v = m.group(1), m.group(2).strip()
        if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
            v = v[1:-1]
        out[k] = v
    return out


def _write_env_file(path: Path, kv: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# Managed by praxdaily — edit through the 设置 tab"]
    for k, v in sorted(kv.items()):
        # Quote values containing spaces/specials; prax's parser unquotes them.
        if any(c in v for c in (" ", "#", '"', "'")):
            v = '"' + v.replace('"', '\\"') + '"'
        lines.append(f"{k}={v}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        # Best-effort — Windows / weird FS don't support chmod, not worth crashing.
        pass


def _user_models_path() -> Path:
    return Path.home() / ".prax" / "models.yaml"


def _load_user_models() -> dict:
    path = _user_models_path()
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise HTTPException(500, f"~/.prax/models.yaml has invalid YAML: {e}")
    return data if isinstance(data, dict) else {}


def _save_user_models(data: dict) -> None:
    path = _user_models_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")


def _provider_base_url_override(models_yaml: dict, provider: str) -> str:
    """Return the user-set base_url for `provider`, or '' if not overridden.

    We read from `~/.prax/models.yaml` only — bundled defaults are never
    surfaced as "user configured" because we don't want users to think
    they wrote them.
    """
    return str(((models_yaml.get("providers") or {}).get(provider) or {}).get("base_url") or "")


def _workspace_models_path(cwd) -> Path:
    return Path(os.fspath(cwd)) / ".prax" / "models.yaml"


def _load_workspace_models(cwd) -> dict:
    """Load `<cwd>/.prax/models.yaml` if it exists. Read-only.

    Why we care: prax merges this layer ON TOP of `~/.prax/models.yaml`,
    so any field set here silently overrides what the user thinks they
    configured through this settings page. We surface that as a conflict
    so users aren't left wondering why their relay base_url isn't taking
    effect.
    """
    path = _workspace_models_path(cwd)
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        # Don't crash the settings page just because workspace yaml is broken;
        # report it as a conflict the user can act on.
        return {"__parse_error__": True}
    return data if isinstance(data, dict) else {}


def _detect_conflict(spec: dict, user_models: dict, workspace_models: dict) -> dict | None:
    """If workspace yaml is overriding what the user set at user level, return
    a conflict dict; else None.

    A conflict only fires when the user has explicitly set a base_url at
    user level AND the workspace layer is setting a *different* value.
    No user-level setting → nothing to conflict with (workspace just wins
    on top of bundled, which is by design).
    """
    if workspace_models.get("__parse_error__"):
        return {
            "kind": "parse_error",
            "message": "workspace 级 .prax/models.yaml YAML 格式错了，prax 会忽略它的内容（但仍占位）",
        }
    user_url = _provider_base_url_override(user_models, spec["provider"])
    ws_url = _provider_base_url_override(workspace_models, spec["provider"])
    if user_url and ws_url and user_url != ws_url:
        return {
            "kind": "base_url_overridden",
            "user_value": user_url,
            "workspace_value": ws_url,
            "message": (
                f"workspace 级 .prax/models.yaml 把 {spec['provider']}.base_url 设成 "
                f"{ws_url!r}，覆盖了你在这里设置的 {user_url!r}。多半是之前跑过 "
                f"prax /init-models 留下的『幽灵文件』。"
            ),
        }
    return None


def _mask(value: str) -> str:
    """Return a never-leaks-the-secret preview: '…' + last 4 chars."""
    if not value:
        return ""
    if len(value) <= 4:
        return "…" + value
    return "…" + value[-4:]


@router.get("/env")
def list_env(request: Request) -> dict[str, Any]:
    cwd = request.app.state.cwd
    path = _env_path(cwd)
    stored = _parse_env_file(path)
    user_models = _load_user_models()
    workspace_models = _load_workspace_models(cwd)

    keys = []
    for spec in KNOWN_KEYS:
        v = stored.get(spec["name"], "")
        override = _provider_base_url_override(user_models, spec["provider"])
        ws_override = _provider_base_url_override(workspace_models, spec["provider"])
        # effective = whichever layer prax actually uses at runtime
        # (workspace > user > bundled default)
        effective = ws_override or override or spec["default_base_url"]
        keys.append({
            **spec,
            "configured": bool(v),
            "preview": _mask(v),
            "base_url_override": override,         # what user set in ~/.prax/models.yaml (or '')
            "effective_base_url": effective,
            "conflict": _detect_conflict(spec, user_models, workspace_models),
        })

    # Surface unknown keys the user wrote manually so we don't lose them
    # when they save through the UI.
    extras = []
    for name, value in stored.items():
        if name in KNOWN_KEY_NAMES:
            continue
        extras.append({
            "name": name,
            "configured": bool(value),
            "preview": _mask(value),
        })

    return {
        "path": str(path),
        "exists": path.exists(),
        "models_yaml_path": str(_user_models_path()),
        "workspace_models_yaml_path": str(_workspace_models_path(cwd)),
        "workspace_models_yaml_exists": _workspace_models_path(cwd).exists(),
        "keys": keys,
        "extras": extras,
    }


@router.post("/cleanup-workspace-yaml")
def cleanup_workspace_yaml(request: Request) -> dict[str, Any]:
    """Back up and remove `<cwd>/.prax/models.yaml` so user-level config
    can take effect. Never deletes outright — always leaves a `.bak.<ts>`
    file so the user can restore if they actually meant to override.
    """
    from datetime import datetime

    cwd = request.app.state.cwd
    path = _workspace_models_path(cwd)
    if not path.exists():
        raise HTTPException(404, "no workspace .prax/models.yaml to clean up")

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = path.with_suffix(f".yaml.bak.{ts}")
    path.rename(backup)
    return {
        "ok": True,
        "removed": str(path),
        "backup": str(backup),
    }


@router.put("/env")
def set_env(body: SetEnvBody, request: Request) -> dict[str, Any]:
    cwd = request.app.state.cwd
    path = _env_path(cwd)

    if not re.match(r"^[A-Z][A-Z0-9_]*$", body.name):
        raise HTTPException(400, "env name must be UPPER_SNAKE_CASE")

    stored = _parse_env_file(path)
    if body.value:
        stored[body.name] = body.value
    else:
        stored.pop(body.name, None)

    _write_env_file(path, stored)
    return {"ok": True, "name": body.name, "configured": bool(body.value)}


@router.put("/provider")
def set_provider(body: SetProviderBody) -> dict[str, Any]:
    """Set / unset the base_url override for a provider.

    Writes only the changed field into ~/.prax/models.yaml — prax's
    config_merge.merge_providers handles the rest at runtime, so the rest
    of the bundled provider definition (format / models / api_key_env)
    stays intact.
    """
    if body.provider not in KNOWN_PROVIDERS:
        raise HTTPException(400, f"unknown provider {body.provider!r}, must be one of {sorted(KNOWN_PROVIDERS)}")

    base_url = body.base_url.strip()
    if base_url and not (base_url.startswith("http://") or base_url.startswith("https://")):
        raise HTTPException(400, "base_url must start with http:// or https://")

    data = _load_user_models()
    providers = data.setdefault("providers", {}) if isinstance(data.get("providers"), dict) or "providers" not in data else data["providers"]
    if not isinstance(providers, dict):
        raise HTTPException(500, "~/.prax/models.yaml `providers` is not a mapping — please fix manually")

    pcfg = providers.get(body.provider) or {}
    if not isinstance(pcfg, dict):
        raise HTTPException(500, f"~/.prax/models.yaml `providers.{body.provider}` is not a mapping — please fix manually")

    if base_url:
        pcfg["base_url"] = base_url
        providers[body.provider] = pcfg
    else:
        # Remove the override; if the provider entry is now empty, drop it
        # entirely so we don't leave dead `providers: openai: {}` stanzas.
        pcfg.pop("base_url", None)
        if pcfg:
            providers[body.provider] = pcfg
        else:
            providers.pop(body.provider, None)

    # If we ended up with an empty providers map and nothing else, write
    # nothing — keep the file pristine.
    if not data.get("providers") and len(data) == 1:
        data.pop("providers")

    if data:
        _save_user_models(data)
    elif _user_models_path().exists():
        _user_models_path().unlink()

    return {"ok": True, "provider": body.provider, "base_url": base_url}


@router.post("/probe")
def probe_base_url(body: ProbeBody, request: Request) -> dict[str, Any]:
    """Verify the relay actually answers at the right URL.

    Why we built this: a soxio user (the original report) typed
    `https://apikey.soxio.me/openai` (missing `/v1`) → prax dispatched
    requests to `/openai/chat/completions` → 404, but the symptoms only
    showed up at cron time as `circuit_breaker`. This endpoint catches
    the typo immediately.

    Strategy: try GET `/models` first (works for OpenAI / Anthropic
    official). Many relays (soxio, oneapi-style) only expose the
    inference endpoints, so on 404 we fall back to a minimal POST
    `/chat/completions` (or `/v1/messages` for Anthropic). Any 4xx /
    5xx response other than 404 means "URL is right, something else is
    off" — that's a successful URL probe even if auth fails.

    Errors are returned in-band with `ok: false` rather than HTTP 5xx
    because a probe failure isn't a server bug, it's user info.
    """
    import httpx

    cwd = request.app.state.cwd
    spec = next((k for k in KNOWN_KEYS if k["provider"] == body.provider), None)
    if spec is None:
        raise HTTPException(400, f"unknown provider {body.provider!r}")

    base = body.base_url.rstrip("/")
    if not (base.startswith("http://") or base.startswith("https://")):
        return {"ok": False, "error": "base_url must start with http:// or https://"}

    api_key = _parse_env_file(_env_path(cwd)).get(spec["name"], "")
    headers = {}
    if api_key:
        if body.provider == "anthropic":
            headers["x-api-key"] = api_key
            headers["anthropic-version"] = "2023-06-01"
        else:
            headers["Authorization"] = f"Bearer {api_key}"

    def _classify(status: int, attempted_url: str) -> dict[str, Any] | None:
        """Return a final result dict if status is conclusive, else None."""
        if 200 <= status < 300:
            return {"ok": True, "url_tried": attempted_url, "http_status": status, "message": "✓ 通了"}
        if status in (400, 401, 403, 405, 422):
            # Endpoint exists; auth/payload/method off — URL is right.
            return {
                "ok": True, "url_tried": attempted_url, "http_status": status,
                "message": f"✓ 端点存在（HTTP {status}：URL 没错，可能是 key/请求格式的问题）",
            }
        return None  # 404 / 5xx / unexpected — let caller decide

    attempts: list[tuple[str, str, dict | None]] = []  # (method, url, json_body)
    if body.provider == "anthropic":
        attempts.append(("POST", base + "/v1/messages", {
            "model": "claude-sonnet-4", "max_tokens": 1, "messages": [{"role": "user", "content": "."}],
        }))
    else:
        attempts.append(("GET", base + "/models", None))
        attempts.append(("POST", base + "/chat/completions", {
            "model": "gpt-4", "messages": [{"role": "user", "content": "."}], "max_tokens": 1, "stream": True,
        }))

    last_status = None
    last_url = None
    try:
        with httpx.Client(timeout=8.0, follow_redirects=False) as c:
            for method, url, json_body in attempts:
                last_url = url
                if method == "GET":
                    r = c.get(url, headers=headers)
                else:
                    r = c.post(url, headers=headers, json=json_body)
                last_status = r.status_code
                hit = _classify(r.status_code, url)
                if hit is not None:
                    return hit
                # 404 → try next attempt; else break with the unexpected status
                if r.status_code != 404:
                    break
    except httpx.ConnectError as e:
        return {"ok": False, "url_tried": last_url, "error": f"连不上：{e}"}
    except httpx.HTTPError as e:
        return {"ok": False, "url_tried": last_url, "error": f"网络错误：{e}"}

    # All attempts exhausted without an "exists" signal.
    if last_status == 404:
        hint = ""
        if not base.endswith("/v1") and "/v1" not in base:
            hint = f"。建议试试 {base}/v1"
        return {
            "ok": False, "url_tried": last_url, "http_status": 404,
            "error": f"端点不存在（HTTP 404）{hint}",
        }
    return {
        "ok": False, "url_tried": last_url, "http_status": last_status,
        "error": f"非预期响应 HTTP {last_status}",
    }


@router.get("/doctor")
def run_doctor(request: Request) -> dict[str, Any]:
    """Shell out to ``prax doctor all`` so the GUI shows what actually works.

    We capture stdout+stderr verbatim because the CLI's per-target output is
    already shaped for humans — re-parsing it here would just create drift.
    """
    cwd = request.app.state.cwd
    try:
        proc = subprocess.run(
            ["prax", "doctor", "all"],
            cwd=os.fspath(cwd),
            capture_output=True,
            text=True,
            timeout=15,
        )
    except FileNotFoundError:
        raise HTTPException(503, "prax CLI not on PATH — install via `npm install -g praxagent`")
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "prax doctor timed out (>15s)")

    return {
        "exit_code": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
    }
