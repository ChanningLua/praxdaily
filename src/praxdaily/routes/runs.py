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
async def list_runs(
    request: Request,
    name: str = "",
    status: str = "",
    limit: int = 100,
) -> JSONResponse:
    """List cron-run logs, newest first.

    Query params (all optional):
      name=<substring>       case-insensitive substring match on job name
      status=success|failure|unknown
                             filter by inferred status; reads each log's
                             content (cheap — small files), so it's slower
                             than name-only filtering on big histories
      limit=<int>            cap result count (default 100, max 1000)

    The total count *before* filtering is always returned so the GUI can
    show "12 of 87" when a search narrows results.
    """
    cwd = request.app.state.cwd
    logs_dir = _logs_dir(cwd)
    limit = max(1, min(1000, limit))

    name_q = name.strip().lower()
    status_q = status.strip().lower()
    valid_statuses = {"success", "failure", "unknown", ""}
    if status_q not in valid_statuses:
        # Validate even when no logs exist — a typo in the query string
        # should always 400 so callers learn fast.
        raise HTTPException(
            status_code=400,
            detail=f"invalid status filter {status!r}; use one of "
            f"{sorted(valid_statuses - {''})}",
        )

    if not logs_dir.exists():
        return JSONResponse({"runs": [], "total": 0, "filtered_total": 0})

    raw: list[dict] = []
    for path in logs_dir.glob("*.log"):
        parsed = _parse_filename(path.name)
        if parsed is None:
            continue
        job_name, ts = parsed
        try:
            stat = path.stat()
        except OSError:
            continue
        raw.append(
            {
                "filename": path.name,
                "name": job_name,
                "started_at": ts,
                "size_bytes": stat.st_size,
                "_path": path,
            }
        )

    raw.sort(key=lambda r: r["started_at"], reverse=True)
    total = len(raw)

    # Name filter is cheap — apply first.
    if name_q:
        raw = [r for r in raw if name_q in r["name"].lower()]

    # Status filter requires reading log bodies. Only do it if explicitly
    # asked (avoids spurious IO on the common "no filter" path).
    if status_q:
        with_status: list[dict] = []
        for r in raw:
            try:
                body = r["_path"].read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if _infer_status(body) == status_q:
                with_status.append(r)
        raw = with_status

    filtered_total = len(raw)
    raw = raw[:limit]

    return JSONResponse(
        {
            "runs": [
                {k: v for k, v in r.items() if not k.startswith("_")}
                for r in raw
            ],
            "total": total,
            "filtered_total": filtered_total,
        }
    )


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
