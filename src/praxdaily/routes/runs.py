"""Read-only view onto cron run logs.

The prax dispatcher writes one log file per run at
``<cwd>/.prax/logs/cron/<name>-<YYYYMMDD-HHMMSS>.log``. This route
just lists them and serves the file content — no parsing, no
reconstruction; what you see is exactly what hit disk.

Status is inferred conservatively from the log body (look for
``status: success`` / ``--- stderr ---`` markers). It's good enough
for a "did this run complete or blow up?" glance; users who need
exact exit codes go open the file.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse


router = APIRouter(prefix="/api/runs", tags=["runs"])


_LOG_NAME_RE = re.compile(r"^(?P<name>.+?)-(?P<stamp>\d{8}-\d{6})\.log$")


def _logs_dir(cwd) -> Path:
    return Path(cwd) / ".prax" / "logs" / "cron"


def _parse_filename(filename: str) -> tuple[str, str] | None:
    """Return (job_name, iso_timestamp) or None if filename doesn't match."""
    m = _LOG_NAME_RE.match(filename)
    if not m:
        return None
    name = m.group("name")
    stamp = m.group("stamp")
    try:
        dt = datetime.strptime(stamp, "%Y%m%d-%H%M%S")
    except ValueError:
        return None
    return name, dt.isoformat()


def _infer_status(body: str) -> str:
    """Infer 'success' / 'failure' / 'unknown' from the log body."""
    if not body:
        return "unknown"
    # The dispatcher writes "Dispatched ... <name>: success (exit 0)" but
    # only into its own stdout, NOT the per-job log. Per-job logs end
    # with a "--- stderr ---" section iff the prompt subprocess wrote to
    # stderr; treat presence of typical failure markers as failure.
    failure_markers = (
        "llm_call failure",
        "Traceback (most recent call last)",
        "Error:",
        "RuntimeError",
    )
    if any(m in body for m in failure_markers):
        return "failure"
    return "success"


@router.get("")
async def list_runs(request: Request) -> JSONResponse:
    """List all cron-run logs, newest first."""
    cwd = request.app.state.cwd
    logs_dir = _logs_dir(cwd)
    if not logs_dir.exists():
        return JSONResponse({"runs": []})

    runs: list[dict] = []
    for path in logs_dir.glob("*.log"):
        parsed = _parse_filename(path.name)
        if parsed is None:
            continue
        name, ts = parsed
        try:
            stat = path.stat()
        except OSError:
            continue
        runs.append(
            {
                "filename": path.name,
                "name": name,
                "started_at": ts,
                "size_bytes": stat.st_size,
            }
        )
    # Newest first by parsed timestamp — sorting by filename alone would
    # mix jobs (`beta-2026-01` would appear before `alpha-2026-04`).
    runs.sort(key=lambda r: r["started_at"], reverse=True)
    return JSONResponse({"runs": runs})


@router.get("/{filename}")
async def get_run(filename: str, request: Request) -> JSONResponse:
    """Return one log file's content + inferred status.

    Path traversal blocked: ``..`` and ``/`` rejected, and the resolved
    path must live under ``<cwd>/.prax/logs/cron/``.
    """
    if "/" in filename or "\\" in filename or filename.startswith(".."):
        raise HTTPException(status_code=400, detail="invalid filename")

    cwd = request.app.state.cwd
    logs_dir = _logs_dir(cwd)
    target = (logs_dir / filename).resolve()

    # Defence-in-depth: even if filename slipped past the syntactic check,
    # the resolved path must stay inside the logs directory.
    try:
        target.relative_to(logs_dir.resolve())
    except ValueError:
        raise HTTPException(status_code=400, detail="path outside logs dir")

    if not target.exists():
        raise HTTPException(status_code=404, detail="log not found")

    try:
        body = target.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"read error: {exc}")

    parsed = _parse_filename(filename) or ("unknown", "")
    return JSONResponse(
        {
            "filename": filename,
            "name": parsed[0],
            "started_at": parsed[1],
            "size_bytes": len(body.encode("utf-8")),
            "status": _infer_status(body),
            "content": body,
        }
    )
