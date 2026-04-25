"""Cron CRUD + dispatcher install / uninstall / run-once.

Reads/writes ``<cwd>/.prax/cron.yaml`` directly to keep the file format
identical to what the prax CLI expects (see
``prax/core/cron_store.py``). Dispatcher install/uninstall and the
"run all due jobs now" trigger shell out to the ``prax cron`` CLI so
we don't duplicate LaunchAgent / crontab / scheduler logic.

Schedule strings are stored verbatim as 5-field cron expressions —
the GUI offers preset frequency pickers that compile down to the same
underlying string, so what you save here matches what
``prax cron list`` / ``prax cron run`` see.
"""

from __future__ import annotations

import shutil
import subprocess
from typing import Any

import yaml
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field


router = APIRouter(prefix="/api/cron", tags=["cron"])


VALID_NOTIFY_TRIGGERS = {"success", "failure"}


def _cron_yaml_path(cwd):
    from pathlib import Path

    return Path(cwd) / ".prax" / "cron.yaml"


def _load_jobs(cwd) -> list[dict[str, Any]]:
    path = _cron_yaml_path(cwd)
    if not path.exists():
        return []
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise HTTPException(status_code=500, detail=f"failed to parse {path}: {exc}")
    jobs = data.get("jobs") if isinstance(data, dict) else None
    return jobs if isinstance(jobs, list) else []


def _save_jobs(cwd, jobs: list[dict[str, Any]]) -> None:
    path = _cron_yaml_path(cwd)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump({"jobs": jobs}, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


# ── pydantic ────────────────────────────────────────────────────────────────


class CronJobUpsert(BaseModel):
    schedule: str = Field(..., description="5-field cron expression")
    prompt: str = Field(..., min_length=1)
    notify_on: list[str] | None = Field(default=None)
    notify_channel: str | None = None
    model: str | None = None
    session_id: str | None = None

    def to_yaml_dict(self, name: str) -> dict[str, Any]:
        out: dict[str, Any] = {
            "name": name,
            "schedule": self.schedule,
            "prompt": self.prompt,
        }
        if self.notify_on:
            out["notify_on"] = list(self.notify_on)
        if self.notify_channel:
            out["notify_channel"] = self.notify_channel
        if self.model:
            out["model"] = self.model
        if self.session_id:
            out["session_id"] = self.session_id
        return out


# ── endpoints ───────────────────────────────────────────────────────────────


@router.get("")
async def list_jobs(request: Request) -> JSONResponse:
    cwd = request.app.state.cwd
    jobs = _load_jobs(cwd)
    return JSONResponse({"jobs": jobs})


def _channel_exists(cwd, channel_name: str) -> bool:
    """Check if a notify channel is declared in .prax/notify.yaml.

    Read-only — we don't import praxagent's loader here because praxdaily
    must keep working when only npm-installed and praxagent's Python
    package isn't import-resolvable.
    """
    from pathlib import Path

    path = Path(cwd) / ".prax" / "notify.yaml"
    if not path.exists():
        return False
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return False
    channels = data.get("channels") if isinstance(data, dict) else None
    return isinstance(channels, dict) and channel_name in channels


@router.put("/{name}")
async def upsert_job(
    name: str, payload: CronJobUpsert, request: Request
) -> JSONResponse:
    if not name or "/" in name or "\\" in name:
        raise HTTPException(status_code=400, detail="invalid job name")
    if payload.notify_on:
        bad = [t for t in payload.notify_on if t not in VALID_NOTIFY_TRIGGERS]
        if bad:
            raise HTTPException(
                status_code=400,
                detail=f"invalid notify_on triggers {bad!r}; allowed: {sorted(VALID_NOTIFY_TRIGGERS)}",
            )

    cwd = request.app.state.cwd

    # If the user wired a notify_channel, make sure it actually exists in
    # notify.yaml. Without this guard, the cron job would silently no-op
    # the notify step at run time and the user would have no idea why
    # their phone never buzzed.
    if payload.notify_channel and not _channel_exists(cwd, payload.notify_channel):
        raise HTTPException(
            status_code=400,
            detail=(
                f"notify_channel {payload.notify_channel!r} not found in "
                f".prax/notify.yaml. Add the channel first, or leave "
                f"notify_channel empty."
            ),
        )

    jobs = _load_jobs(cwd)
    new_record = payload.to_yaml_dict(name)
    # Replace existing job with same name, else append.
    jobs = [j for j in jobs if j.get("name") != name]
    jobs.append(new_record)
    _save_jobs(cwd, jobs)
    return JSONResponse(new_record)


@router.delete("/{name}")
async def delete_job(name: str, request: Request) -> JSONResponse:
    cwd = request.app.state.cwd
    jobs = _load_jobs(cwd)
    if not any(j.get("name") == name for j in jobs):
        raise HTTPException(status_code=404, detail=f"job {name!r} not found")
    jobs = [j for j in jobs if j.get("name") != name]
    _save_jobs(cwd, jobs)
    return JSONResponse({"deleted": name})


# ── dispatcher control (shell-outs to prax cron) ────────────────────────────


def _run_prax_cron(*subcmd: str, cwd: str) -> tuple[int, str]:
    """Invoke `prax cron <subcmd>` and return (returncode, combined_output)."""
    prax = shutil.which("prax")
    if prax is None:
        raise HTTPException(
            status_code=503,
            detail="prax CLI not on PATH — install with `npm install -g praxagent`",
        )
    proc = subprocess.run(
        [prax, "cron", *subcmd],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=120,
    )
    return proc.returncode, (proc.stdout or proc.stderr or "").strip()


@router.post("/install")
async def install_dispatcher(request: Request) -> JSONResponse:
    """Run `prax cron install` (LaunchAgent on macOS, crontab line on Linux)."""
    rc, output = _run_prax_cron("install", cwd=request.app.state.cwd)
    if rc != 0:
        raise HTTPException(status_code=500, detail=output or "install failed")
    return JSONResponse({"installed": True, "output": output})


@router.post("/uninstall")
async def uninstall_dispatcher(request: Request) -> JSONResponse:
    rc, output = _run_prax_cron("uninstall", cwd=request.app.state.cwd)
    if rc != 0:
        raise HTTPException(status_code=500, detail=output or "uninstall failed")
    return JSONResponse({"uninstalled": True, "output": output})


@router.post("/run-once")
async def run_once_now(request: Request) -> JSONResponse:
    """Fire all DUE jobs once (same semantics as `prax cron run`).

    Note: jobs whose schedule doesn't match the current minute are NOT
    fired here. To force-run a specific job regardless of schedule, see
    `POST /api/cron/{name}/trigger-now`.
    """
    rc, output = _run_prax_cron("run", cwd=request.app.state.cwd)
    if rc != 0:
        raise HTTPException(status_code=500, detail=output or "run failed")
    return JSONResponse({"dispatched": True, "output": output})


@router.post("/{name}/trigger-now")
async def trigger_job_now(name: str, request: Request) -> JSONResponse:
    """Force-run one job's prompt RIGHT NOW, ignoring its schedule.

    Goes through `prax prompt <prompt>` directly with workspace-write —
    same code path the cron dispatcher uses for the actual subprocess.
    Bypasses notify_on/notify_channel because the user explicitly
    triggered it and is staring at the GUI; if they want a notify they
    can hit "立刻跑一次" instead (which respects schedule + notify).
    """
    import shutil
    import subprocess

    cwd = request.app.state.cwd
    jobs = _load_jobs(cwd)
    job = next((j for j in jobs if j.get("name") == name), None)
    if job is None:
        raise HTTPException(status_code=404, detail=f"job {name!r} not found")

    prompt = str(job.get("prompt") or "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail=f"job {name!r} has empty prompt")

    prax = shutil.which("prax")
    if prax is None:
        raise HTTPException(
            status_code=503,
            detail="prax CLI not on PATH — install with `npm install -g praxagent`",
        )

    argv = [prax, "prompt", prompt, "--permission-mode", "workspace-write"]
    try:
        proc = subprocess.run(
            argv, cwd=str(cwd), capture_output=True, text=True, timeout=600
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(
            status_code=504,
            detail=f"job {name!r} exceeded 10-minute trigger budget",
        )

    output = (proc.stdout or proc.stderr or "").strip()
    return JSONResponse(
        {
            "triggered": True,
            "name": name,
            "exit_code": proc.returncode,
            "output_tail": output[-2000:],
        }
    )
