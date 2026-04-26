"""Workspace registry endpoints — list / add / select / remove cwds.

The active cwd flows from ``~/.praxdaily/workspaces.json`` (managed by
``praxdaily.workspaces``). All other routes resolve cwd via
``request.app.state.cwd``, which is a thin proxy that reads the live
selection on every access.

Adding a workspace also selects it (one-step from the GUI).
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ..workspaces import (
    add_workspace,
    load_state,
    remove_workspace,
    select_workspace,
)


router = APIRouter(prefix="/api/workspaces", tags=["workspaces"])


def _default(request: Request) -> str:
    """Server-launch default cwd, used to seed an empty registry."""
    return str(request.app.state.default_cwd)


class PathPayload(BaseModel):
    path: str


@router.get("")
async def list_workspaces(request: Request) -> JSONResponse:
    state = load_state(default_cwd=_default(request))
    return JSONResponse(state)


@router.post("")
async def register_workspace(payload: PathPayload, request: Request) -> JSONResponse:
    """Add a new workspace and select it."""
    try:
        state = add_workspace(payload.path, default_cwd=_default(request))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return JSONResponse(state)


@router.post("/select")
async def select_workspace_endpoint(payload: PathPayload, request: Request) -> JSONResponse:
    """Switch the active workspace. Path must already be registered."""
    try:
        state = select_workspace(payload.path, default_cwd=_default(request))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return JSONResponse(state)


@router.delete("")
async def unregister_workspace(request: Request, path: str = "") -> JSONResponse:
    """Drop a workspace from the registry."""
    if not path:
        raise HTTPException(status_code=400, detail="?path= is required")
    state = remove_workspace(path, default_cwd=_default(request))
    return JSONResponse(state)
