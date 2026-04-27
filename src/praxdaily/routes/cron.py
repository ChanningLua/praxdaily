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


_AI_NEWS_TRIGGER_TOKENS = ("ai-news-daily", "ai日报", "ai 日报", "每日简报", "每日 ai", "daily digest")


def _is_ai_news_daily_prompt(prompt: str) -> bool:
    """Detect whether this prompt is targeting the flagship daily-digest
    workflow, regardless of phrasing. Liberal matching is fine — false
    positives just route through the deterministic pipeline (which is
    arguably better than the LLM path for any 'fetch+summarize+push'
    request anyway)."""
    p = prompt.lower()
    return any(t in p for t in _AI_NEWS_TRIGGER_TOKENS)


@router.post("/{name}/trigger-now")
async def trigger_job_now(name: str, request: Request) -> JSONResponse:
    """Force-run one job's prompt RIGHT NOW, ignoring its schedule.

    For the flagship `ai-news-daily` prompt we run praxdaily's native
    pipeline (deterministic Python scrapers + direct notify). The old
    "shell out to prax + LLM follows skill instructions" path was too
    flaky — autocli/Chrome dependencies, circuit breakers, recursive
    self-invocation. For any other prompt, fall back to shelling out
    to prax so user-added jobs still work.
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

    # Native fast path for ai-news-daily trigger phrases.
    if _is_ai_news_daily_prompt(prompt):
        from .. import pipeline
        result = await pipeline.run(cwd=cwd)
        # Map pipeline result onto the same response shape the GUI already
        # expects from trigger-now (triggered/exit_code/output_tail/notify).
        ok = not result.fatal_error and result.notify.get("sent")
        summary_lines = [
            f"started: {result.started_at} → {result.finished_at}",
            f"digest: {result.digest_chars} chars → {result.digest_path}",
        ]
        for sr in result.sources:
            if not sr.enabled:
                summary_lines.append(f"  - {sr.id}: disabled (skipped)")
            elif sr.error:
                summary_lines.append(f"  ✗ {sr.id}: {sr.error}")
            else:
                summary_lines.append(f"  ✓ {sr.id}: fetched={sr.fetched} kept={sr.kept}")
        if result.fatal_error:
            summary_lines.append(f"FATAL: {result.fatal_error}")
        if result.notify:
            summary_lines.append(f"notify: {result.notify}")
        return JSONResponse(
            {
                "triggered": True,
                "name": name,
                "exit_code": 0 if ok else 1,
                "output_tail": "\n".join(summary_lines),
                "notify": result.notify,
                "pipeline": result.to_dict(),
            }
        )

    prax = shutil.which("prax")
    if prax is None:
        raise HTTPException(
            status_code=503,
            detail="prax CLI not on PATH — install with `npm install -g praxagent`",
        )

    # Use danger-full-access for ad-hoc triggers because realistic skill
    # work (ai-news-daily uses tmux + autocli + Chrome state) trips
    # workspace-write's InteractiveBash gate. The user clicked "立即触发"
    # so they explicitly opted into "run the whole pipeline" — matching
    # what they'd want at scheduled time too.
    argv = [prax, "prompt", prompt, "--permission-mode", "danger-full-access"]
    # If the job pinned a model, pass it so trigger-now matches the cron
    # dispatcher behaviour (otherwise prax tier-routing might escalate to
    # an unconfigured model and 401).
    job_model = str(job.get("model") or "").strip()
    if job_model:
        argv += ["--model", job_model]
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
    notify_status = await _maybe_send_notify(cwd, job, proc.returncode, output)

    return JSONResponse(
        {
            "triggered": True,
            "name": name,
            "exit_code": proc.returncode,
            "output_tail": output[-2000:],
            "notify": notify_status,
        }
    )


async def _maybe_send_notify(cwd, job: dict, exit_code: int, output: str) -> dict[str, Any]:
    """Mirror the cron dispatcher's notify behaviour for ad-hoc triggers.

    Returns a status dict the GUI can show: whether a notify was attempted,
    which channel, and any error. Never raises — a notify failure must not
    flip the trigger response into 5xx, since the prompt itself succeeded.
    """
    triggers = [str(t).strip().lower() for t in (job.get("notify_on") or [])]
    channel_name = str(job.get("notify_channel") or "").strip()
    if not triggers or not channel_name:
        return {"sent": False, "reason": "no notify_on or notify_channel configured"}

    outcome = "success" if exit_code == 0 else "failure"
    if outcome not in triggers:
        return {"sent": False, "reason": f"outcome={outcome} not in notify_on={triggers}"}

    # Lazy imports — keep startup time clean and let the route 503 cleanly
    # if praxagent isn't installed.
    try:
        from prax.tools.notify import build_provider  # type: ignore
    except ImportError as exc:
        return {"sent": False, "channel": channel_name, "error": f"praxagent not importable: {exc}"}

    from .channels import _load_channels
    channels = _load_channels(cwd)
    if channel_name not in channels:
        return {"sent": False, "channel": channel_name, "error": "channel not found in notify.yaml"}

    try:
        provider = build_provider(channels[channel_name])
    except ValueError as exc:
        return {"sent": False, "channel": channel_name, "error": str(exc)}

    title = f"{'✓' if exit_code == 0 else '✗'} {job.get('name')} 触发{'成功' if exit_code == 0 else '失败'} (exit {exit_code})"
    # Trim body to the tail of stdout — same as cron dispatcher does, so
    # what arrives in WeChat matches what the user would see at scheduled time.
    body = output[-1500:] if output else "(no output)"
    try:
        await provider.send(title=title, body=body, level=("info" if exit_code == 0 else "error"))
    except Exception as exc:  # noqa: BLE001 — we want any error surfaced as status, not 500
        return {"sent": False, "channel": channel_name, "error": f"{type(exc).__name__}: {exc}"}

    return {"sent": True, "channel": channel_name, "outcome": outcome}
