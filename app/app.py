from __future__ import annotations

import asyncio
from collections import deque
from typing import Any, Deque, Dict

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse
from loguru import logger

from app.config import Settings, get_settings
from app.idempotency import IdempotencyCache
from app.models import ClosePayload, EntryPayload, StatusSnapshot, WebhookResponse
from app.services.gmo import GmoCoinService
from app.services.notifier import DiscordNotifier
from app.utils import parse_iso8601_z, quantize, utcnow

app = FastAPI(title="TradingView GMOcoin Bot", version="1.0.0")


class AppState:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.idempotency = IdempotencyCache(ttl_seconds=settings.idempotency_ttl_seconds)
        self.gmo = GmoCoinService(settings)
        self.notifier = DiscordNotifier(settings.discord_webhook_url)
        self.operation_lock = asyncio.Lock()
        self.last_events: Deque[Dict[str, Any]] = deque(maxlen=settings.status_cache_size)


@app.on_event("startup")
async def on_startup(settings: Settings = Depends(get_settings)) -> None:
    app.state.runtime = AppState(settings)
    logger.info("Starting services for environment=%s", settings.env)
    await app.state.runtime.gmo.start()
    await app.state.runtime.notifier.start()


@app.on_event("shutdown")
async def on_shutdown() -> None:
    runtime: AppState = app.state.runtime
    await runtime.gmo.stop()
    await runtime.notifier.stop()


@app.get("/healthz")
async def healthz() -> Dict[str, str]:
    return {"status": "ok"}


@app.get("/status", response_model=StatusSnapshot)
async def status_endpoint() -> StatusSnapshot:
    runtime: AppState = app.state.runtime
    position = await runtime.gmo.get_position("BTC_JPY")
    events = list(runtime.last_events)
    return StatusSnapshot(
        position_size=position.size,
        position_side=position.side,
        last_events=events,
        websocket_connected=runtime.gmo.websocket_connected,
        retries=runtime.gmo.retry_stats,
    )


@app.post("/webhook", response_model=WebhookResponse)
async def webhook_endpoint(request: Request) -> JSONResponse:
    payload_data = await request.json()
    mode = payload_data.get("mode")
    runtime: AppState = app.state.runtime
    settings = runtime.settings

    if mode == "ENTRY":
        payload = EntryPayload(**payload_data)
    elif mode == "CLOSE":
        payload = ClosePayload(**payload_data)
    else:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid mode")

    if payload.token != settings.webhook_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")

    if payload.symbol not in settings.allowed_symbol_set:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Symbol not allowed")

    event_time = parse_iso8601_z(payload.ts)
    now = utcnow()
    skew = abs((now - event_time).total_seconds())
    if skew > settings.max_skew_seconds:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Event timestamp skew too large")

    added = await runtime.idempotency.add(payload.event_id, payload.mode, payload.symbol)
    if not added:
        logger.info("Duplicate event received event_id=%s", payload.event_id)
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content=WebhookResponse(
                status="duplicate",
                detail="Event already processed",
                ignored=True,
                event_id=payload.event_id,
            ).dict(),
        )

    latency_ms = int((now - event_time).total_seconds() * 1000)

    if mode == "ENTRY":
        response = await _handle_entry(runtime, payload, latency_ms)
    else:
        response = await _handle_close(runtime, payload, latency_ms)
    return response


async def _handle_entry(runtime: AppState, payload: EntryPayload, latency_ms: int) -> JSONResponse:
    size = quantize(payload.size, runtime.settings.qty_step)
    if size <= 0:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Size below minimum step")

    async with runtime.operation_lock:
        position = await runtime.gmo.get_position(payload.symbol)
        if position.size > 0:
            logger.info(
                "ENTRY ignored due to existing position size=%s side=%s", position.size, position.side
            )
            return JSONResponse(
                status_code=status.HTTP_200_OK,
                content=WebhookResponse(
                    status="ignored",
                    detail="Position already open",
                    ignored=True,
                    event_id=payload.event_id,
                ).dict(),
            )
        try:
            response = await runtime.gmo.place_entry(payload.symbol, payload.side, size)
        except Exception as exc:  # noqa: BLE001
            await runtime.notifier.notify(
                f"ERROR | id={payload.event_id} mode=ENTRY msg={exc}"
            )
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    avg_price = _extract_average_price(response)
    message = (
        f"ENTRY OK | id={payload.event_id} sym={payload.symbol} side={payload.side} "
        f"size={size:.8f} avg_px={avg_price or 'n/a'} latency={latency_ms}ms"
    )
    await runtime.notifier.notify(message)
    _record_event(runtime, {
        "event_id": payload.event_id,
        "mode": payload.mode,
        "side": payload.side,
        "size": size,
        "timestamp": utcnow().isoformat(),
        "message": message,
    })
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content=WebhookResponse(
            status="ok",
            detail="Entry executed",
            ignored=False,
            event_id=payload.event_id,
        ).dict(),
    )


async def _handle_close(runtime: AppState, payload: ClosePayload, latency_ms: int) -> JSONResponse:
    async with runtime.operation_lock:
        position = await runtime.gmo.get_position(payload.symbol)
        if position.size <= 0:
            logger.info("CLOSE noop no open position")
            message = (
                f"CLOSE OK | id={payload.event_id} sym={payload.symbol} closed=0 avg_px=n/a "
                f"pnl=0 latency={latency_ms}ms"
            )
            await runtime.notifier.notify(message)
            _record_event(runtime, {
                "event_id": payload.event_id,
                "mode": payload.mode,
                "closed": 0,
                "timestamp": utcnow().isoformat(),
                "message": message,
            })
            return JSONResponse(
                status_code=status.HTTP_200_OK,
                content=WebhookResponse(
                    status="ok",
                    detail="No position to close",
                    ignored=False,
                    event_id=payload.event_id,
                ).dict(),
            )
        close_side = "SELL" if position.side == "BUY" else "BUY"
        try:
            response = await runtime.gmo.close_position(payload.symbol, close_side, position.size)
        except Exception as exc:  # noqa: BLE001
            await runtime.notifier.notify(
                f"ERROR | id={payload.event_id} mode=CLOSE msg={exc}"
            )
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    avg_price = _extract_average_price(response)
    pnl = _extract_pnl(response)
    message = (
        f"CLOSE OK | id={payload.event_id} sym={payload.symbol} closed={position.size:.8f} "
        f"avg_px={avg_price or 'n/a'} pnl={pnl if pnl is not None else 'n/a'} latency={latency_ms}ms"
    )
    await runtime.notifier.notify(message)
    _record_event(runtime, {
        "event_id": payload.event_id,
        "mode": payload.mode,
        "closed": position.size,
        "timestamp": utcnow().isoformat(),
        "message": message,
    })
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content=WebhookResponse(
            status="ok",
            detail="Position closed",
            ignored=False,
            event_id=payload.event_id,
        ).dict(),
    )


def _record_event(runtime: AppState, event: Dict[str, Any]) -> None:
    runtime.last_events.append(event)


def _extract_average_price(response: Dict[str, Any]) -> Any:
    data = response.get("data") if isinstance(response, dict) else None
    if isinstance(data, dict):
        return data.get("price") or data.get("avgPrice") or data.get("averagePrice")
    if isinstance(data, list) and data:
        candidate = data[0]
        if isinstance(candidate, dict):
            return candidate.get("price") or candidate.get("avgPrice") or candidate.get("averagePrice")
    return None


def _extract_pnl(response: Dict[str, Any]) -> Any:
    data = response.get("data") if isinstance(response, dict) else None
    if isinstance(data, dict):
        return data.get("pnl") or data.get("profitLoss")
    if isinstance(data, list) and data:
        candidate = data[0]
        if isinstance(candidate, dict):
            return candidate.get("pnl") or candidate.get("profitLoss")
    return None
