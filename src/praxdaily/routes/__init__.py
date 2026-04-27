"""HTTP route modules for the praxdaily web panel.

Each module exposes a ``router: APIRouter`` that ``app.create_app``
mounts under ``/api``. Splitting them out keeps ``app.py`` skimmable
and makes per-route tests obvious.
"""

from .channels import router as channels_router
from .cron import router as cron_router
from .runs import router as runs_router
from .schedule import router as schedule_router
from .settings import router as settings_router
from .sources import router as sources_router
from .wechat import router as wechat_router
from .workspaces import router as workspaces_router

__all__ = [
    "channels_router",
    "cron_router",
    "runs_router",
    "schedule_router",
    "settings_router",
    "sources_router",
    "wechat_router",
    "workspaces_router",
]
