"""WeChat account management — list, login (QR), logout.

Login is split into two stateless endpoints so the GUI can render the
QR code + poll for confirmation without holding a server-side session:

  POST /api/wechat/login/start
       → {qrcode_value, qrcode_url, base_url}
  POST /api/wechat/login/poll  body: {qrcode_value, base_url}
       → {status, redirect_host?, account_id?, user_id?}

When the iLink server says ``confirmed`` we save the credentials to
``~/.prax/wechat/<account_id>.json`` (mode 0600) — same path the
``prax wechat login`` CLI writes to. After that, the new account
appears in the regular ``/api/wechat/accounts`` list and is selectable
from the Channels form.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel


logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/wechat", tags=["wechat"])


def _import_wechat_module():
    """Centralize the praxagent import + helpful error.

    Returns the module or raises HTTPException(503) with install hint.
    """
    try:
        from prax.integrations import wechat_ilink
        return wechat_ilink
    except ImportError as exc:
        raise HTTPException(
            status_code=503,
            detail=(
                f"praxagent not importable — install with "
                f"`npm install -g praxagent`. ({exc})"
            ),
        )


@router.get("/accounts")
async def list_wechat_accounts() -> JSONResponse:
    """Return all iLink accounts saved on this machine."""
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


# ── QR login: start ──────────────────────────────────────────────────────────


class LoginStartResponse(BaseModel):
    qrcode_value: str
    qrcode_url: str
    base_url: str


@router.post("/login/start")
async def login_start(bot_type: str = "3") -> JSONResponse:
    """Fetch a fresh QR from iLink. Client renders + polls separately.

    The returned ``qrcode_value`` is the opaque hex token iLink uses to
    track this scan attempt; the client passes it back unchanged on
    every poll. The ``qrcode_url`` is a scannable WeChat liteapp URL
    that should be encoded into the visible QR pattern.
    """
    import httpx

    wx = _import_wechat_module()
    from prax.integrations.wechat_ilink.client import (
        EP_GET_BOT_QR,
        ILINK_BASE_URL,
        QR_TIMEOUT_MS,
        _api_get,
    )

    try:
        async with httpx.AsyncClient(trust_env=True) as client:
            qr_resp = await _api_get(
                client,
                base_url=ILINK_BASE_URL,
                endpoint=f"{EP_GET_BOT_QR}?bot_type={bot_type}",
                timeout_ms=QR_TIMEOUT_MS,
            )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"iLink QR fetch failed: {exc}")

    qrcode_value = str(qr_resp.get("qrcode") or "")
    qrcode_url = str(qr_resp.get("qrcode_img_content") or "")
    if not qrcode_value:
        raise HTTPException(
            status_code=502, detail="iLink returned no qrcode token"
        )

    return JSONResponse(
        {
            "qrcode_value": qrcode_value,
            # If iLink omits a scannable URL we fall back to the bare
            # token; some WeChat versions accept it directly.
            "qrcode_url": qrcode_url or qrcode_value,
            "base_url": ILINK_BASE_URL,
        }
    )


# ── QR login: poll ───────────────────────────────────────────────────────────


class LoginPollPayload(BaseModel):
    qrcode_value: str
    base_url: str  # client tracks redirect_host from previous polls


@router.post("/login/poll")
async def login_poll(payload: LoginPollPayload) -> JSONResponse:
    """Single-shot poll — client should call this every ~2 s until terminal.

    iLink statuses we relay verbatim:
      wait                  — QR not yet scanned
      scaned                — user opened it in WeChat, confirmation pending
      scaned_but_redirect   — server is steering us to a regional host;
                              client must use returned ``redirect_host``
                              for next poll
      expired               — client should call /login/start for a new QR
      confirmed             — credentials are saved; account is now in
                              /api/wechat/accounts
    """
    import httpx

    wx = _import_wechat_module()
    from prax.integrations.wechat_ilink.client import (
        EP_GET_QR_STATUS,
        ILINK_BASE_URL,
        QR_TIMEOUT_MS,
        _api_get,
    )
    from prax.integrations.wechat_ilink.store import save_account

    base_url = payload.base_url or ILINK_BASE_URL

    try:
        async with httpx.AsyncClient(trust_env=True) as client:
            status_resp = await _api_get(
                client,
                base_url=base_url,
                endpoint=f"{EP_GET_QR_STATUS}?qrcode={payload.qrcode_value}",
                timeout_ms=QR_TIMEOUT_MS,
            )
    except (httpx.ReadTimeout, httpx.ConnectTimeout):
        # Treat as transient "wait" — the client just polls again.
        return JSONResponse({"status": "wait"})
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"iLink poll failed: {exc}")

    status = str(status_resp.get("status") or "wait")

    if status == "scaned_but_redirect":
        redirect_host = str(status_resp.get("redirect_host") or "")
        return JSONResponse(
            {
                "status": status,
                "redirect_host": redirect_host,
                "next_base_url": f"https://{redirect_host}" if redirect_host else base_url,
            }
        )

    if status == "confirmed":
        account_id = str(status_resp.get("ilink_bot_id") or "")
        token = str(status_resp.get("bot_token") or "")
        confirmed_base_url = str(status_resp.get("baseurl") or base_url)
        user_id = str(status_resp.get("ilink_user_id") or "")
        if not account_id or not token:
            raise HTTPException(
                status_code=502,
                detail="iLink confirmed but credential payload incomplete",
            )
        save_account(
            account_id=account_id,
            token=token,
            base_url=confirmed_base_url,
            user_id=user_id,
        )
        # IMPORTANT: never leak the bot_token back to the browser.
        return JSONResponse(
            {
                "status": "confirmed",
                "account_id": account_id,
                "user_id": user_id,
            }
        )

    return JSONResponse({"status": status})


# ── logout ──────────────────────────────────────────────────────────────────


@router.delete("/accounts/{account_id}")
async def delete_wechat_account(account_id: str) -> JSONResponse:
    """Remove a saved iLink account credential file."""
    if "/" in account_id or "\\" in account_id or account_id.startswith(".."):
        raise HTTPException(status_code=400, detail="invalid account_id")

    wx = _import_wechat_module()
    if not wx.delete_account(account_id):
        raise HTTPException(status_code=404, detail=f"account {account_id!r} not found")
    return JSONResponse({"deleted": account_id})
