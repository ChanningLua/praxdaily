"""praxdaily-owned schedule (LaunchAgent) management.

Replaces the old `prax cron install/uninstall` flow that the GUI used
to drive. The legacy dispatcher would route scheduled runs through
``prax prompt`` + the ai-news-daily skill — flaky and behaviourally
divergent from the manual trigger path. This route owns its own
LaunchAgent that calls ``praxdaily run-now`` directly.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field


router = APIRouter(prefix="/api/schedule", tags=["schedule"])


class InstallScheduleBody(BaseModel):
    time: str = Field(default="14:00", description="HH:MM, 24h")
    # cwd is taken from request.app.state.cwd so the GUI's active workspace
    # automatically wins; callers don't supply it.


@router.get("")
def get_schedule_status() -> dict[str, Any]:
    """Current install state + legacy prax-cron detection."""
    from .. import scheduler

    return {
        "praxdaily": scheduler.status(),
        "legacy_prax_cron": scheduler.detect_prax_cron_dispatcher(),
    }


@router.post("/install")
def install_schedule(body: InstallScheduleBody, request: Request) -> dict[str, Any]:
    from .. import scheduler

    cwd = request.app.state.cwd
    try:
        sched = scheduler.Schedule.parse_hhmm(body.time)
    except ValueError as e:
        raise HTTPException(400, str(e))

    try:
        return scheduler.install(schedule=sched, cwd=cwd)
    except NotImplementedError as e:
        raise HTTPException(501, str(e))


@router.delete("")
def uninstall_schedule() -> dict[str, Any]:
    from .. import scheduler

    try:
        return scheduler.uninstall()
    except NotImplementedError as e:
        raise HTTPException(501, str(e))


@router.post("/uninstall-legacy-prax-cron")
def uninstall_legacy() -> dict[str, Any]:
    """Remove the old `dev.prax.cron.dispatcher` plist if present.

    Surfaced as a separate action so the GUI can show a clear "you have
    a legacy scheduler that conflicts; click here to remove" prompt
    without coupling that decision to installing the new one.
    """
    from .. import scheduler

    return scheduler.uninstall_prax_cron_dispatcher()
