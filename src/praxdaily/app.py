"""FastAPI app — the local web panel.

For the 0.1.0 milestone this only exposes ``GET /api/health`` and serves
a placeholder HTML shell that proves the wrapper is up. The real five
screens (sources / schedule / channels / runs / setup) land in 0.2.x as
each one's API contract is specced in ``docs/plans/0.6-daily-gui.md``
in the prax repo.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__


WEB_DIR = Path(__file__).parent / "web"


def create_app(cwd: Path) -> FastAPI:
    app = FastAPI(
        title="praxdaily",
        version=__version__,
        description="Local web panel for Prax's ai-news-daily flagship workflow.",
    )
    # ``app.state.cwd`` is the *server-launch* default. The active
    # workspace is chosen separately and looked up via
    # ``praxdaily.workspaces.current_cwd(default_cwd=str(cwd))`` —
    # routes call ``_resolve_cwd(request)`` rather than reading
    # app.state.cwd directly.
    app.state.cwd = cwd
    # Patch app.state.cwd to dynamically resolve the active workspace,
    # so existing routes that read ``request.app.state.cwd`` get the
    # current selection without code changes.
    from .workspaces import current_cwd as _current_workspace_cwd

    class _LiveCwd:
        """Mimics a Path-like for ``str()`` and ``/`` operations."""
        def __init__(self, default_cwd: Path):
            self._default = str(default_cwd)
        def _now(self) -> Path:
            return Path(_current_workspace_cwd(default_cwd=self._default))
        def __str__(self) -> str:
            return str(self._now())
        def __fspath__(self) -> str:
            return str(self._now())
        def __truediv__(self, other) -> Path:
            return self._now() / other
        @property
        def exists(self):
            return self._now().exists

    app.state.cwd = _LiveCwd(cwd)
    # Keep the unwrapped seed available for the workspaces route.
    app.state.default_cwd = cwd

    # Mount API route modules.
    from .routes import (
        channels_router, cron_router, runs_router, sources_router,
        wechat_router, workspaces_router,
    )
    app.include_router(channels_router)
    app.include_router(cron_router)
    app.include_router(runs_router)
    app.include_router(sources_router)
    app.include_router(wechat_router)
    app.include_router(workspaces_router)

    @app.get("/api/health")
    async def health() -> JSONResponse:
        prax_path = shutil.which("prax")
        prax_version: str | None = None
        if prax_path:
            try:
                proc = subprocess.run(
                    [prax_path, "--version"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                prax_version = (proc.stdout or proc.stderr).strip() or None
            except Exception:
                pass
        active_cwd = Path(str(app.state.cwd))
        return JSONResponse(
            {
                "praxdaily_version": __version__,
                "cwd": str(active_cwd),
                "default_cwd": str(app.state.default_cwd),
                "prax_on_path": prax_path,
                "prax_version": prax_version,
                "prax_dir_exists": (active_cwd / ".prax").exists(),
            }
        )

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(WEB_DIR / "index.html")

    if WEB_DIR.exists():
        app.mount(
            "/static",
            StaticFiles(directory=str(WEB_DIR)),
            name="static",
        )

    return app


def serve(*, host: str, port: int, cwd: Path) -> None:
    """Start the uvicorn server (blocking)."""
    import uvicorn

    app = create_app(cwd)
    uvicorn.run(app, host=host, port=port, log_level="info")
