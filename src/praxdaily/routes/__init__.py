"""HTTP route modules for the praxdaily web panel.

Each module exposes a ``router: APIRouter`` that ``app.create_app``
mounts under ``/api``. Splitting them out keeps ``app.py`` skimmable
and makes per-route tests obvious.
"""

from .channels import router as channels_router
from .cron import router as cron_router
from .runs import router as runs_router
from .wechat import router as wechat_router

__all__ = ["channels_router", "cron_router", "runs_router", "wechat_router"]
