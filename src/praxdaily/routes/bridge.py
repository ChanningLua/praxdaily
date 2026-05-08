"""WeChat bridge REST endpoints.

Mounts under ``/api/bridge`` for personas / bots / bindings / messages,
plus ``/api/wechat/webhook`` for inbound iLink events (real or
simulated).

The application layer (e.g. ``apps.daily_qa``) consumes the bridge
through these endpoints: APP backend hits ``/api/bridge/messages/send``
or per-persona ``send``, the bridge resolves the binding to a wxid and
dispatches via iLink. Inbound replies flow the other way through the
webhook handler.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .. import bridge


logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/bridge", tags=["bridge"])


@router.get("/health")
async def bridge_health(request: Request) -> JSONResponse:
    """Liveness summary for the bridge platform."""
    cwd = request.app.state.cwd
    summary: dict[str, Any] = {"status": "ok"}
    try:
        with bridge.connect(cwd) as conn:
            summary["personas"] = len(bridge.personas.list_all(conn))
            summary["bots"] = len(bridge.bots.list_all(conn))
            summary["active_bindings"] = len(bridge.bindings.list_active(conn))
            summary["msgs_total"] = conn.execute(
                "SELECT COUNT(*) FROM messages"
            ).fetchone()[0]
    except Exception as exc:  # noqa: BLE001
        summary["status"] = "degraded"
        summary["error"] = f"{type(exc).__name__}: {exc}"
    mgr = getattr(request.app.state, "puller_manager", None)
    if mgr is not None:
        running = sum(1 for p in mgr.pullers.values() if p.is_running)
        summary["pullers_running"] = running
        summary["pullers_total"] = len(mgr.pullers)
    return JSONResponse(summary)

# Inbound iLink webhook lives under /api/wechat/* alongside login.
webhook_router = APIRouter(prefix="/api/wechat", tags=["wechat-webhook"])


# ── pydantic models ─────────────────────────────────────────────────────────


class PersonaUpsert(BaseModel):
    name: str
    avatar_url: str = ""
    intro: str = ""
    card_image_path: str = ""
    is_default: bool = False


class BotRegister(BaseModel):
    wxid: str
    persona_id: str
    ilink_account_id: str
    role: str = "primary"
    capacity: int = 3000


class BindingCreate(BaseModel):
    app_user_id: str
    persona_id: str
    target_wxid: str
    bot_wxid: str | None = None
    note: str = ""


class SendMessage(BaseModel):
    app_user_id: str
    content: str
    idempotency_key: str | None = None


DEFAULT_DAILY_PERSONA_ID = "_daily"


class QrcodeRequest(BaseModel):
    """Subscribe-to-daily-news payload.

    ``persona_id`` is optional; when absent we fall back to the system
    ``_daily`` persona, which the bridge auto-creates the first time a
    subscriber QR is generated. Multi-persona apps can still pass an
    explicit persona_id, but the daily-news flow doesn't expose this in
    its UI.
    """
    app_user_id: str
    persona_id: str | None = None
    bot_wxid: str | None = None
    ttl_minutes: int = 10


def _ensure_default_persona(conn) -> str:
    """Create the system ``_daily`` persona if missing. Returns its id."""
    try:
        bridge.personas.get(conn, DEFAULT_DAILY_PERSONA_ID)
    except KeyError:
        bridge.personas.upsert(
            conn,
            persona_id=DEFAULT_DAILY_PERSONA_ID,
            name="日报",
            intro="praxdaily 日报订阅",
            is_default=True,
        )
    return DEFAULT_DAILY_PERSONA_ID


class InboundEvent(BaseModel):
    """Simulated or real iLink inbound payload.

    For demo we accept a flat ``{bot_wxid, target_wxid, content}`` shape.
    Real iLink webhook payloads can be normalized into this on the way in
    once the actual format is confirmed.
    """
    bot_wxid: str
    target_wxid: str
    content: str
    event_type: str = "receive_message"


# ── personas ────────────────────────────────────────────────────────────────


@router.get("/personas")
async def list_personas(request: Request) -> JSONResponse:
    cwd = request.app.state.cwd
    with bridge.connect(cwd) as conn:
        items = [p.to_dict() for p in bridge.personas.list_all(conn)]
    return JSONResponse({"personas": items})


@router.put("/personas/{persona_id}")
async def upsert_persona(
    persona_id: str, payload: PersonaUpsert, request: Request
) -> JSONResponse:
    if not persona_id or "/" in persona_id or "\\" in persona_id:
        raise HTTPException(status_code=400, detail="invalid persona_id")
    cwd = request.app.state.cwd
    with bridge.connect(cwd) as conn:
        try:
            p = bridge.personas.upsert(
                conn,
                persona_id=persona_id,
                name=payload.name,
                avatar_url=payload.avatar_url,
                intro=payload.intro,
                card_image_path=payload.card_image_path,
                is_default=payload.is_default,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
    return JSONResponse(p.to_dict())


@router.delete("/personas/{persona_id}")
async def delete_persona(persona_id: str, request: Request) -> JSONResponse:
    cwd = request.app.state.cwd
    with bridge.connect(cwd) as conn:
        bridge.personas.delete(conn, persona_id)
    return JSONResponse({"deleted": persona_id})


# ── bots ────────────────────────────────────────────────────────────────────


@router.get("/bots")
async def list_bots(request: Request) -> JSONResponse:
    cwd = request.app.state.cwd
    with bridge.connect(cwd) as conn:
        items = [b.to_dict() for b in bridge.bots.list_all(conn)]
    return JSONResponse({"bots": items})


@router.post("/bots")
async def register_bot(payload: BotRegister, request: Request) -> JSONResponse:
    cwd = request.app.state.cwd
    with bridge.connect(cwd) as conn:
        # Validate persona exists.
        try:
            bridge.personas.get(conn, payload.persona_id)
        except KeyError:
            raise HTTPException(
                status_code=404, detail=f"persona {payload.persona_id!r} not found"
            )
        try:
            b = bridge.bots.register(
                conn,
                wxid=payload.wxid,
                persona_id=payload.persona_id,
                ilink_account_id=payload.ilink_account_id,
                role=payload.role,
                capacity=payload.capacity,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
    return JSONResponse(b.to_dict())


@router.delete("/bots/{wxid}/{persona_id}")
async def unregister_bot(wxid: str, persona_id: str, request: Request) -> JSONResponse:
    cwd = request.app.state.cwd
    with bridge.connect(cwd) as conn:
        bridge.bots.unregister(conn, wxid=wxid, persona_id=persona_id)
    return JSONResponse({"deleted": {"wxid": wxid, "persona_id": persona_id}})


# ── bindings ────────────────────────────────────────────────────────────────


@router.get("/bindings")
async def list_bindings(
    request: Request,
    app_user_id: str | None = None,
    persona_id: str | None = None,
) -> JSONResponse:
    cwd = request.app.state.cwd
    with bridge.connect(cwd) as conn:
        if app_user_id and persona_id:
            try:
                b = bridge.bindings.get_active(
                    conn, app_user_id=app_user_id, persona_id=persona_id
                )
                items = [b.to_dict()]
            except KeyError:
                items = []
        elif app_user_id:
            items = [b.to_dict() for b in bridge.bindings.list_for_user(conn, app_user_id)]
        elif persona_id:
            items = [b.to_dict() for b in bridge.bindings.list_for_persona(conn, persona_id)]
        else:
            raise HTTPException(
                status_code=400, detail="pass app_user_id and/or persona_id"
            )
    return JSONResponse({"bindings": items})


@router.post("/bindings")
async def create_binding(payload: BindingCreate, request: Request) -> JSONResponse:
    cwd = request.app.state.cwd
    with bridge.connect(cwd) as conn:
        try:
            b = bridge.bindings.manual_bind(
                conn,
                app_user_id=payload.app_user_id,
                persona_id=payload.persona_id,
                target_wxid=payload.target_wxid,
                bot_wxid=payload.bot_wxid,
                note=payload.note,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
    return JSONResponse(b.to_dict())


# ── binding via QR ──────────────────────────────────────────────────────────


def _auto_attach_first_ilink_bot(conn, persona_id: str) -> str | None:
    """If the persona has no registered bot, pick the first logged-in
    iLink account and register it on the fly. Returns the chosen
    ilink_account_id (or None if no accounts exist).

    The ``wxid`` we store equals iLink's ``user_id`` (the bot account's
    actual WeChat ID), since that's what inbound webhooks will report.
    """
    if bridge.bots.list_active_for_persona(conn, persona_id):
        return None
    try:
        from prax.integrations.wechat_ilink import list_accounts  # type: ignore
    except ImportError:
        return None
    accounts = list(list_accounts())
    if not accounts:
        return None
    first = accounts[0]
    bot_wxid = first.user_id or first.account_id
    bridge.bots.register(
        conn,
        wxid=bot_wxid,
        persona_id=persona_id,
        ilink_account_id=first.account_id,
    )
    return first.account_id


@router.post("/binding/qrcode")
async def binding_qrcode(payload: QrcodeRequest, request: Request) -> JSONResponse:
    """Mint a token + return everything the UI needs to render a QR.

    Auto-attaches the persona to the first logged-in iLink account when
    no bot has been registered yet — so the typical flow ("define
    persona → click 扫码绑定") works with zero pre-config. Operators
    who want fine-grained control can still pre-register bots via the
    bots panel and pass ``bot_wxid`` explicitly.
    """
    cwd = request.app.state.cwd
    with bridge.connect(cwd) as conn:
        # Validate persona exists.
        try:
            persona = bridge.personas.get(conn, payload.persona_id)
        except KeyError:
            raise HTTPException(
                status_code=404, detail=f"persona {payload.persona_id!r} not found"
            )
        auto_attached = _auto_attach_first_ilink_bot(conn, payload.persona_id)
        try:
            bt = bridge.binding_tokens.create(
                conn,
                app_user_id=payload.app_user_id,
                persona_id=payload.persona_id,
                bot_wxid=payload.bot_wxid,
                ttl_minutes=payload.ttl_minutes,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    # The QR encodes the token text plain so any QR scanner (incl. the
    # iOS WeChat scan-and-send-as-text path) hands the user the right
    # 6-char string.
    qr_payload = bt.token
    instruction = (
        f"打开微信，进入「{persona.name}」对应的机器人聊天窗口，"
        f"发送这串验证码：{bt.token}"
    )
    return JSONResponse({
        "token": bt.token,
        "qr_payload": qr_payload,
        "instruction": instruction,
        "persona_id": bt.persona_id,
        "persona_name": persona.name,
        "bot_wxid": bt.bot_wxid,
        "ilink_account_id": bt.ilink_account_id,
        "expires_at": bt.expires_at,
        "auto_attached_bot": auto_attached,
    })


@router.get("/binding/status")
async def binding_status(request: Request, token: str) -> JSONResponse:
    cwd = request.app.state.cwd
    with bridge.connect(cwd) as conn:
        bt = bridge.binding_tokens.get_or_none(conn, token)
    if bt is None:
        raise HTTPException(status_code=404, detail="token not found")
    return JSONResponse({
        "token": bt.token,
        "status": bt.status,
        "binding_id": bt.binding_id,
        "expires_at": bt.expires_at,
        "completed_at": bt.completed_at,
        "persona_id": bt.persona_id,
        "app_user_id": bt.app_user_id,
    })


# ── iLink-QR-backed binding (real WeChat-scannable QR) ──────────────────────


@router.post("/binding/qr-start")
async def binding_qr_start(payload: QrcodeRequest, request: Request) -> JSONResponse:
    """Mint a binding token AND grab a fresh iLink login QR.

    Implementation note: WeChat won't recognize plain-text QRs, so we
    can't ship a "scan to bind" QR by ourselves. iLink/praxagent only
    exposes one WeChat-scannable QR — the bot login QR. We re-purpose it
    here: the user scans, iLink confirms, and we capture their
    ``ilink_user_id`` (their WeChat address) to populate the binding's
    ``target_wxid`` automatically.

    Side effect: a new "bot account" record is registered in iLink under
    the scanning user's WeChat. The caller never touches that record;
    sending continues to flow through the original clawbot account.
    """
    import httpx
    from prax.integrations.wechat_ilink.client import (
        EP_GET_BOT_QR, ILINK_BASE_URL, QR_TIMEOUT_MS, _api_get,
    )

    cwd = request.app.state.cwd
    with bridge.connect(cwd) as conn:
        # Daily-news flow: caller doesn't need to know about personas.
        # Pick the system default and auto-create on first use.
        persona_id = payload.persona_id or _ensure_default_persona(conn)
        try:
            persona = bridge.personas.get(conn, persona_id)
        except KeyError:
            raise HTTPException(
                status_code=404, detail=f"persona {persona_id!r} not found"
            )
        _auto_attach_first_ilink_bot(conn, persona_id)
        try:
            bt = bridge.binding_tokens.create(
                conn,
                app_user_id=payload.app_user_id,
                persona_id=persona_id,
                bot_wxid=payload.bot_wxid,
                ttl_minutes=payload.ttl_minutes,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

        # Fetch a fresh QR from iLink.
        try:
            async with httpx.AsyncClient(trust_env=True) as client:
                qr_resp = await _api_get(
                    client,
                    base_url=ILINK_BASE_URL,
                    endpoint=f"{EP_GET_BOT_QR}?bot_type=3",
                    timeout_ms=QR_TIMEOUT_MS,
                )
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"iLink QR fetch failed: {exc}")

        qrcode_value = str(qr_resp.get("qrcode") or "")
        qrcode_url = str(qr_resp.get("qrcode_img_content") or "") or qrcode_value
        if not qrcode_value:
            raise HTTPException(status_code=502, detail="iLink returned no qrcode token")

        bridge.binding_tokens.attach_ilink_qrcode(
            conn, token=bt.token, qrcode_value=qrcode_value, base_url=ILINK_BASE_URL
        )

    return JSONResponse({
        "token": bt.token,
        "qrcode_value": qrcode_value,
        "qrcode_url": qrcode_url,
        "base_url": ILINK_BASE_URL,
        "persona_id": bt.persona_id,
        "persona_name": persona.name,
        "expires_at": bt.expires_at,
    })


class QrPollPayload(BaseModel):
    token: str
    base_url: str | None = None


@router.post("/binding/qr-poll")
async def binding_qr_poll(payload: QrPollPayload, request: Request) -> JSONResponse:
    """Single poll. Client should call every ~2s until terminal.

    Statuses pass-through from iLink: ``wait`` / ``scaned`` /
    ``scaned_but_redirect`` / ``expired`` / ``confirmed``. On
    ``confirmed`` we capture ``ilink_user_id`` (the scanner's WeChat ID)
    and finalize the binding atomically.
    """
    import httpx
    from prax.integrations.wechat_ilink.client import (
        EP_GET_QR_STATUS, ILINK_BASE_URL, QR_TIMEOUT_MS, _api_get,
    )

    cwd = request.app.state.cwd
    with bridge.connect(cwd) as conn:
        bt = bridge.binding_tokens.get_or_none(conn, payload.token)
    if bt is None:
        raise HTTPException(status_code=404, detail="token not found")
    if not bt.ilink_qrcode_value:
        raise HTTPException(status_code=400, detail="token has no iLink QR — call qr-start first")
    if bt.status == "bound":
        return JSONResponse({"status": "confirmed", "binding_id": bt.binding_id, "already_bound": True})
    if bt.status == "expired":
        return JSONResponse({"status": "expired"})

    base_url = payload.base_url or bt.ilink_base_url or ILINK_BASE_URL
    try:
        async with httpx.AsyncClient(trust_env=True) as client:
            status_resp = await _api_get(
                client,
                base_url=base_url,
                endpoint=f"{EP_GET_QR_STATUS}?qrcode={bt.ilink_qrcode_value}",
                timeout_ms=QR_TIMEOUT_MS,
            )
    except (httpx.ReadTimeout, httpx.ConnectTimeout):
        return JSONResponse({"status": "wait"})
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"iLink poll failed: {exc}")

    status = str(status_resp.get("status") or "wait")

    if status == "scaned_but_redirect":
        redirect_host = str(status_resp.get("redirect_host") or "")
        return JSONResponse({
            "status": status,
            "redirect_host": redirect_host,
            "next_base_url": f"https://{redirect_host}" if redirect_host else base_url,
        })

    if status == "confirmed":
        # The scanning user's WeChat address is iLink's ``ilink_user_id``.
        ilink_user_id = str(status_resp.get("ilink_user_id") or "")
        ilink_bot_id = str(status_resp.get("ilink_bot_id") or "")
        if not ilink_user_id:
            raise HTTPException(status_code=502, detail="iLink confirmed but no ilink_user_id")

        # Save the scanner's account credentials too — iLink expects this for
        # any future bot ops. We don't actually use this account for sending
        # (clawbot keeps doing that); this is a side effect of reusing the
        # login QR mechanism.
        token = str(status_resp.get("bot_token") or "")
        confirmed_base = str(status_resp.get("baseurl") or base_url)
        if ilink_bot_id and token:
            try:
                from prax.integrations.wechat_ilink.store import save_account
                save_account(account_id=ilink_bot_id, token=token,
                             base_url=confirmed_base, user_id=ilink_user_id)
            except Exception:
                pass  # non-fatal — binding still works with clawbot

        with bridge.connect(cwd) as conn:
            try:
                _bt, binding_id = bridge.binding_tokens.finalize_with_ilink_user(
                    conn, token=bt.token, user_wxid=ilink_user_id,
                )
            except ValueError as exc:
                # Token already consumed (race) — surface gently.
                cur = bridge.binding_tokens.get_or_none(conn, bt.token)
                return JSONResponse({
                    "status": "confirmed",
                    "binding_id": cur.binding_id if cur else None,
                    "warning": str(exc),
                })
        return JSONResponse({
            "status": "confirmed",
            "binding_id": binding_id,
            "scanner_wxid": ilink_user_id,
        })

    return JSONResponse({"status": status})


# ── send + inbox ────────────────────────────────────────────────────────────


@router.post("/personas/{persona_id}/send")
async def send_message(
    persona_id: str, payload: SendMessage, request: Request
) -> JSONResponse:
    cwd = request.app.state.cwd
    with bridge.connect(cwd) as conn:
        try:
            persona = bridge.personas.get(conn, persona_id)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"persona {persona_id!r} not found")
        try:
            binding = bridge.bindings.get_active(
                conn, app_user_id=payload.app_user_id, persona_id=persona_id
            )
        except KeyError:
            raise HTTPException(
                status_code=404,
                detail=f"no binding for ({payload.app_user_id}, {persona_id}) — bind first",
            )
        result = await bridge.send.send_via_binding(
            conn,
            persona=persona,
            binding=binding,
            content=payload.content,
            idempotency_key=payload.idempotency_key,
        )
    if not result.get("sent"):
        # Persisted to DB but the network call failed — surface 502 so the
        # APP backend retries or alerts. The msg_id is still returned for tracing.
        return JSONResponse(result, status_code=502)
    return JSONResponse(result)


@router.get("/personas/{persona_id}/inbox")
async def list_inbox(
    persona_id: str,
    request: Request,
    app_user_id: str | None = None,
    limit: int = 50,
) -> JSONResponse:
    cwd = request.app.state.cwd
    with bridge.connect(cwd) as conn:
        try:
            bridge.personas.get(conn, persona_id)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"persona {persona_id!r} not found")
        items = bridge.inbound.list_inbox(
            conn, persona_id=persona_id, app_user_id=app_user_id, limit=limit
        )
    return JSONResponse({"messages": items})


# ── subscribers (active bindings + activity stats) ─────────────────────────


@router.get("/subscribers")
async def list_subscribers(request: Request) -> JSONResponse:
    """All active bindings with light usage stats — feeds the dashboard
    "订阅者" tab. For each user we count messages sent in/out and surface
    the most recent activity timestamp.
    """
    cwd = request.app.state.cwd
    items = []
    with bridge.connect(cwd) as conn:
        rows = conn.execute(
            """
            SELECT b.binding_id, b.app_user_id, b.bot_wxid, b.target_wxid,
                   b.created_at,
                   (SELECT COUNT(*) FROM messages m
                    WHERE m.app_user_id = b.app_user_id
                          AND m.direction = 'out' AND m.status = 'sent') AS msgs_out,
                   (SELECT COUNT(*) FROM messages m
                    WHERE m.app_user_id = b.app_user_id
                          AND m.direction = 'in') AS msgs_in,
                   (SELECT MAX(created_at) FROM messages m
                    WHERE m.app_user_id = b.app_user_id) AS last_activity_at
            FROM bindings b
            WHERE b.status = 'bound'
            ORDER BY b.created_at DESC
            """
        ).fetchall()
        for r in rows:
            items.append({
                "binding_id": r["binding_id"],
                "app_user_id": r["app_user_id"],
                "bot_wxid": r["bot_wxid"],
                "target_wxid": r["target_wxid"],
                "created_at": r["created_at"],
                "msgs_out": r["msgs_out"],
                "msgs_in": r["msgs_in"],
                "last_activity_at": r["last_activity_at"],
            })
    return JSONResponse({"subscribers": items, "total": len(items)})


@router.delete("/subscribers/{binding_id}")
async def expire_subscriber(binding_id: int, request: Request) -> JSONResponse:
    """Operator force-unsubscribe: mark the binding expired."""
    cwd = request.app.state.cwd
    with bridge.connect(cwd) as conn:
        cur = conn.execute(
            "UPDATE bindings SET status = 'expired' WHERE binding_id = ? AND status = 'bound'",
            (binding_id,),
        )
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="binding not found or already expired")
        conn.commit()
    return JSONResponse({"expired": binding_id})


# ── puller management ───────────────────────────────────────────────────────


@router.get("/pullers")
async def list_pullers(request: Request) -> JSONResponse:
    """Status snapshot for all running iLink getupdates pullers."""
    mgr = getattr(request.app.state, "puller_manager", None)
    if mgr is None:
        return JSONResponse({"pullers": [], "error": "puller_manager not initialized"})
    return JSONResponse({"pullers": mgr.status()})


@router.post("/pullers/sync")
async def sync_pullers(request: Request) -> JSONResponse:
    """Reconcile pullers against currently logged-in iLink accounts.
    Call this after a new login or account deletion."""
    mgr = getattr(request.app.state, "puller_manager", None)
    if mgr is None:
        raise HTTPException(status_code=500, detail="puller_manager not initialized")
    actions = await mgr.sync_with_accounts()
    return JSONResponse({"actions": actions, "pullers": mgr.status()})


# ── inbound webhook ─────────────────────────────────────────────────────────


@webhook_router.post("/webhook")
async def receive_webhook(payload: InboundEvent, request: Request) -> JSONResponse:
    """Accept an inbound iLink event (or a simulated one for demo).

    For demo we expose a hand-crafted payload shape; once we wire real
    iLink callbacks we can normalise the iLink JSON into ``InboundEvent``
    in front of this handler. Returns the routing decision so the
    operator can verify which persona claimed the reply.
    """
    if payload.event_type != "receive_message":
        # Friend requests / system events: stub for now, accept silently.
        return JSONResponse({"accepted": True, "ignored": payload.event_type})

    cwd = request.app.state.cwd
    with bridge.connect(cwd) as conn:
        result = bridge.inbound.receive(
            conn,
            bot_wxid=payload.bot_wxid,
            target_wxid=payload.target_wxid,
            content=payload.content,
        )
    return JSONResponse(result)
