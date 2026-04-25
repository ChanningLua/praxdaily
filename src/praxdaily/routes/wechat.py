"""Read-only view onto saved iLink accounts.

The actual login flow stays in the ``prax wechat login`` CLI — the GUI
just lists what's already in ``~/.prax/wechat/`` so the Channels form
can offer a dropdown of account_ids instead of forcing the user to
hand-copy the long string.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse


router = APIRouter(prefix="/api/wechat", tags=["wechat"])


@router.get("/accounts")
async def list_wechat_accounts() -> JSONResponse:
    """Return all iLink accounts saved by ``prax wechat login``.

    If ``praxagent`` isn't installed (so the integrations import would
    fail) we return an empty list with a hint instead of 500ing — that
    way the GUI can render "no accounts yet, run prax wechat login"
    without having to special-case import errors.
    """
    try:
        from prax.integrations.wechat_ilink import list_accounts
    except ImportError:
        return JSONResponse(
            {
                "accounts": [],
                "hint": "praxagent not importable — install with `npm install -g praxagent`.",
            }
        )

    accounts = list_accounts()
    return JSONResponse(
        {
            "accounts": [
                {
                    "account_id": a.account_id,
                    "user_id": a.user_id,
                    "saved_at": a.saved_at,
                }
                for a in accounts
            ]
        }
    )
