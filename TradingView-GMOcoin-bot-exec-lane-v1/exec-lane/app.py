from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import platform
import time
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN, getcontext
from typing import Any, Dict, List, Optional

import orjson
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from dotenv import dotenv_values
from pydantic import BaseModel, Field, ValidationError, root_validator

import httpx

import gmo
import notify
import storage

if platform.system() == "Linux":  # pragma: no cover - uvloop optional
    try:
        import uvloop

        uvloop.install()
    except Exception:  # pragma: no cover - fallback when uvloop missing
        pass

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("exec-lane")

getcontext().prec = 18


class Settings(BaseModel):
    webhook_token: str = Field(..., alias="WEBHOOK_TOKEN")
    api_key: str = Field(..., alias="GMO_API_KEY")
    api_secret: str = Field(..., alias="GMO_API_SECRET")
    allowed_symbols: List[str] = Field(..., alias="ALLOWED_SYMBOLS")
    entry_policy: str = Field("ignore", alias="ENTRY_POLICY")
    max_skew_seconds: int = Field(60, alias="MAX_SKEW_SECONDS")
    qty_step: Decimal = Field(Decimal("0.01"), alias="QTY_STEP")
    min_qty: Decimal = Field(Decimal("0.01"), alias="MIN_QTY")
    notify_discord_webhook_url: Optional[str] = Field(None, alias="NOTIFY_DISCORD_WEBHOOK_URL")
    env: str = Field("prod", alias="ENV")
    symbol: str = "BTC_JPY"
    idempotency_ttl: int = 600

    @root_validator(pre=True)
    def split_symbols(cls, values: Dict[str, Any]) -> Dict[str, Any]:
        symbols = values.get("ALLOWED_SYMBOLS")
        if isinstance(symbols, str):
            values["ALLOWED_SYMBOLS"] = [s.strip() for s in symbols.split(",") if s.strip()]
        return values


class BaseWebhook(BaseModel):
    token: str
    event_id: str
    ts: datetime
    mode: str
    symbol: str

    @root_validator
    def ensure_mode(cls, values: Dict[str, Any]) -> Dict[str, Any]:
        mode = values.get("mode")
        if mode not in {"ENTRY", "CLOSE"}:
            raise ValueError("mode must be ENTRY or CLOSE")
        return values


class EntryWebhook(BaseWebhook):
    side: str
    size: Decimal

    @root_validator
    def validate_entry(cls, values: Dict[str, Any]) -> Dict[str, Any]:
        side = values.get("side")
        if side not in {"BUY", "SELL"}:
            raise ValueError("side must be BUY or SELL")
        size = values.get("size")
        if size is None:
            raise ValueError("size is required for ENTRY")
        return values


class CloseWebhook(BaseWebhook):
    pass


def load_settings() -> Settings:
    config_path = os.path.join(os.path.dirname(__file__), "..", "config", ".env")
    env_values = {}
    if os.path.exists(config_path):
        env_values.update({k: v for k, v in dotenv_values(config_path).items() if v is not None})
    env_values.update({k: v for k, v in os.environ.items() if v is not None})
    try:
        settings = Settings.parse_obj(env_values)
    except ValidationError as exc:
        raise RuntimeError(f"Invalid configuration: {exc}") from exc
    return settings


settings = load_settings()

app = FastAPI(title="TradingView GMO Coin Exec Lane", version="1.0.0")

runtime_state: Dict[str, Any] = {
    "position_qty": Decimal("0"),
    "position_side": "FLAT",
    "last_event_id": None,
    "last_event_ts": None,
    "events_processed": 0,
    "retry_stats": {},
    "ws_connected": False,
    "ws_reconnects": 0,
    "retries_total": 0,
}

state_lock = asyncio.Lock()
trade_lock = asyncio.Lock()
execution_lock = asyncio.Lock()
execution_events: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
execution_waiters: Dict[str, asyncio.Event] = {}


def json_log(level: str, **payload: Any) -> None:
    line = orjson.dumps(payload).decode()
    log_level = getattr(logging, level.upper(), logging.INFO)
    logger.log(log_level, line)


async def wait_for_order_fill(order_id: Optional[str], timeout: float = 10.0) -> List[Dict[str, Any]]:
    if not order_id:
        return []
    async with execution_lock:
        existing = execution_events.get(order_id)
        if existing:
            return execution_events.pop(order_id)
        event = asyncio.Event()
        execution_waiters[order_id] = event
    try:
        await asyncio.wait_for(execution_waiters[order_id].wait(), timeout)
    except asyncio.TimeoutError:
        return []
    finally:
        async with execution_lock:
            execution_waiters.pop(order_id, None)
    async with execution_lock:
        return execution_events.pop(order_id, [])


async def record_execution_event(message: Dict[str, Any]) -> None:
    events = message.get("data") or []
    async with execution_lock:
        for item in events:
            order_id = str(item.get("orderId") or item.get("order_id") or "")
            if not order_id:
                continue
            execution_events[order_id].append(item)
            waiter = execution_waiters.get(order_id)
            if waiter:
                waiter.set()


async def update_position_state(message: Dict[str, Any]) -> None:
    summaries = message.get("data") or []
    qty = Decimal("0")
    side = "FLAT"
    for summary in summaries:
        if summary.get("symbol") != settings.symbol:
            continue
        size = summary.get("size") or summary.get("holdingQuantity") or 0
        side_value = summary.get("side") or summary.get("positionSide") or "FLAT"
        qty = Decimal(str(size))
        side = str(side_value).upper()
    async with state_lock:
        runtime_state["position_qty"] = qty
        runtime_state["position_side"] = side
    await asyncio.to_thread(storage.set_state, "position_qty", qty)
    await asyncio.to_thread(storage.set_state, "position_side", side)


def compute_avg_price(executions: List[Dict[str, Any]]) -> Optional[float]:
    if not executions:
        return None
    total = Decimal("0")
    qty = Decimal("0")
    for item in executions:
        price = item.get("price") or item.get("executionPrice")
        size = item.get("size") or item.get("executionSize")
        if price is None or size is None:
            continue
        price_dec = Decimal(str(price))
        size_dec = Decimal(str(size))
        total += price_dec * size_dec
        qty += size_dec
    if qty <= 0:
        return None
    return float((total / qty))


def quantize_size(value: Decimal) -> Decimal:
    step = settings.qty_step
    if step <= 0:
        return value
    scaled = (value / step).to_integral_value(rounding=ROUND_DOWN)
    return scaled * step


async def ensure_symbol_allowed(symbol: str) -> None:
    if symbol not in settings.allowed_symbols:
        raise HTTPException(status_code=400, detail="symbol not allowed")


async def check_auth(payload: BaseWebhook) -> None:
    if payload.token != settings.webhook_token:
        raise HTTPException(status_code=401, detail="invalid token")


async def check_freshness(payload: BaseWebhook) -> None:
    ts = payload.ts
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    skew = abs((now - ts).total_seconds())
    if skew > settings.max_skew_seconds:
        raise HTTPException(status_code=400, detail="timestamp skew too large")


async def ensure_idempotent(event_id: str) -> None:
    recent = await asyncio.to_thread(storage.exists_recent, event_id, settings.idempotency_ttl)
    if recent:
        raise HTTPException(status_code=200, detail="duplicate event")
    inserted = await asyncio.to_thread(storage.put_event, event_id)
    if not inserted:
        raise HTTPException(status_code=200, detail="duplicate event")
    await asyncio.to_thread(storage.prune_old_events, settings.idempotency_ttl)


async def process_entry(payload: EntryWebhook) -> JSONResponse:
    await ensure_symbol_allowed(payload.symbol)
    start = time.monotonic()
    async with trade_lock:
        async with state_lock:
            current_qty = runtime_state.get("position_qty", Decimal("0"))
        if current_qty > 0 and settings.entry_policy == "ignore":
            json_log(
                "info",
                event_id=payload.event_id,
                mode="ENTRY",
                symbol=payload.symbol,
                size=float(payload.size),
                result="ignored",
                latency_ms=0,
                reason="position not flat",
            )
            return JSONResponse({"status": "ignored", "reason": "position exists"})

        size = quantize_size(Decimal(str(payload.size)))
        if size < settings.min_qty:
            raise HTTPException(status_code=400, detail="size below minimum")

        try:
            order_data, rest_result = await gmo.market_entry(
                app.state.rest_client,
                settings.api_key,
                settings.api_secret,
                payload.symbol,
                payload.side,
                size,
                logger=logger,
            )
        except gmo.GMORetryableError as err:
            await notify.notify_error(
                settings.notify_discord_webhook_url,
                event_id=payload.event_id,
                mode="ENTRY",
                symbol=payload.symbol,
                attempt=f"{len(getattr(err, 'attempts', []))}/3",
                code=getattr(err, "code", None),
                msg=str(err),
                http_client=app.state.http_client,
            )
            json_log(
                "error",
                event_id=payload.event_id,
                mode="ENTRY",
                symbol=payload.symbol,
                size=float(size),
                result="failure",
                latency_ms=(time.monotonic() - start) * 1000,
                err=str(err),
            )
            raise HTTPException(status_code=502, detail="entry failed") from err
        except gmo.GMOAPIError as err:
            await notify.notify_error(
                settings.notify_discord_webhook_url,
                event_id=payload.event_id,
                mode="ENTRY",
                symbol=payload.symbol,
                attempt="1/1",
                code=err.code,
                msg=str(err),
                http_client=app.state.http_client,
            )
            json_log(
                "error",
                event_id=payload.event_id,
                mode="ENTRY",
                symbol=payload.symbol,
                size=float(size),
                result="failure",
                latency_ms=(time.monotonic() - start) * 1000,
                err=str(err),
            )
            raise HTTPException(status_code=400, detail="entry rejected") from err

        order_info = order_data.get("data") or order_data
        order_id = order_info.get("orderId") or order_info.get("order_id")
        executions = await wait_for_order_fill(order_id)
        avg_price = compute_avg_price(executions)

        total_latency = (time.monotonic() - start) * 1000
        await notify.notify_entry_success(
            settings.notify_discord_webhook_url,
            event_id=payload.event_id,
            symbol=payload.symbol,
            side=payload.side,
            size=float(size),
            avg_px=avg_price,
            latency_ms=total_latency,
            http_client=app.state.http_client,
        )

        async with state_lock:
            runtime_state["last_event_id"] = payload.event_id
            runtime_state["last_event_ts"] = payload.ts.isoformat()
            runtime_state["events_processed"] += 1
            runtime_state["retry_stats"]["entry"] = rest_result.attempts
            runtime_state["retries_total"] += len(rest_result.attempts)

        json_log(
            "info",
            event_id=payload.event_id,
            mode="ENTRY",
            symbol=payload.symbol,
            size=float(size),
            result="success",
            latency_ms=total_latency,
        )
        await asyncio.to_thread(storage.set_state, "last_event_id", payload.event_id)
        await asyncio.to_thread(storage.set_state, "last_event_ts", payload.ts.isoformat())
        return JSONResponse({"status": "ok", "order_id": order_id})


async def process_close(payload: CloseWebhook) -> JSONResponse:
    await ensure_symbol_allowed(payload.symbol)
    start = time.monotonic()
    async with trade_lock:
        positions = await gmo.get_positions(
            app.state.rest_client,
            settings.api_key,
            settings.api_secret,
            payload.symbol,
            logger=logger,
        )
        if not positions:
            await notify.notify_flat(
                settings.notify_discord_webhook_url,
                event_id=payload.event_id,
                symbol=payload.symbol,
                http_client=app.state.http_client,
            )
            json_log(
                "info",
                event_id=payload.event_id,
                mode="CLOSE",
                symbol=payload.symbol,
                size=0,
                result="already_flat",
                latency_ms=0,
            )
            return JSONResponse({"status": "ok", "detail": "already flat"})

        total_size = sum(Decimal(pos.get("size", "0")) for pos in positions)

        try:
            close_data, rest_result = await gmo.market_close_all(
                app.state.rest_client,
                settings.api_key,
                settings.api_secret,
                payload.symbol,
                logger=logger,
            )
        except gmo.GMORetryableError as err:
            await notify.notify_error(
                settings.notify_discord_webhook_url,
                event_id=payload.event_id,
                mode="CLOSE",
                symbol=payload.symbol,
                attempt=f"{len(getattr(err, 'attempts', []))}/3",
                code=getattr(err, "code", None),
                msg=str(err),
                http_client=app.state.http_client,
            )
            json_log(
                "error",
                event_id=payload.event_id,
                mode="CLOSE",
                symbol=payload.symbol,
                size=float(total_size),
                result="failure",
                latency_ms=(time.monotonic() - start) * 1000,
                err=str(err),
            )
            raise HTTPException(status_code=502, detail="close failed") from err
        except gmo.GMOAPIError as err:
            await notify.notify_error(
                settings.notify_discord_webhook_url,
                event_id=payload.event_id,
                mode="CLOSE",
                symbol=payload.symbol,
                attempt="1/1",
                code=err.code,
                msg=str(err),
                http_client=app.state.http_client,
            )
            json_log(
                "error",
                event_id=payload.event_id,
                mode="CLOSE",
                symbol=payload.symbol,
                size=float(total_size),
                result="failure",
                latency_ms=(time.monotonic() - start) * 1000,
                err=str(err),
            )
            raise HTTPException(status_code=400, detail="close rejected") from err

        order_info = close_data.get("data") or close_data
        order_id = order_info.get("orderId") or order_info.get("order_id")
        executions = await wait_for_order_fill(order_id)
        avg_price = compute_avg_price(executions)

        total_latency = (time.monotonic() - start) * 1000
        await notify.notify_close_success(
            settings.notify_discord_webhook_url,
            event_id=payload.event_id,
            symbol=payload.symbol,
            closed_qty=float(total_size),
            avg_px=avg_price,
            realized_pnl=None,
            latency_ms=total_latency,
            http_client=app.state.http_client,
        )

        async with state_lock:
            runtime_state["last_event_id"] = payload.event_id
            runtime_state["last_event_ts"] = payload.ts.isoformat()
            runtime_state["events_processed"] += 1
            runtime_state["retry_stats"]["close"] = rest_result.attempts
            runtime_state["retries_total"] += len(rest_result.attempts)

        json_log(
            "info",
            event_id=payload.event_id,
            mode="CLOSE",
            symbol=payload.symbol,
            size=float(total_size),
            result="success",
            latency_ms=total_latency,
        )
        await asyncio.to_thread(storage.set_state, "last_event_id", payload.event_id)
        await asyncio.to_thread(storage.set_state, "last_event_ts", payload.ts.isoformat())
        return JSONResponse({"status": "ok", "order_id": order_id})


@app.on_event("startup")
async def on_startup() -> None:
    storage.init_db()
    app.state.rest_client = gmo.create_rest_client()
    app.state.http_client = httpx.AsyncClient(timeout=10.0)
    positions = await gmo.get_positions(
        app.state.rest_client,
        settings.api_key,
        settings.api_secret,
        settings.symbol,
        logger=logger,
    )
    total_qty = sum(Decimal(pos.get("size", "0")) for pos in positions)
    side = "FLAT"
    if positions:
        side = positions[0].get("side", "FLAT").upper()
    async with state_lock:
        runtime_state["position_qty"] = total_qty
        runtime_state["position_side"] = side
    await asyncio.to_thread(storage.set_state, "position_qty", total_qty)
    await asyncio.to_thread(storage.set_state, "position_side", side)

    async def position_callback(message: Dict[str, Any]) -> None:
        await update_position_state(message)

    async def execution_callback(message: Dict[str, Any]) -> None:
        await record_execution_event(message)

    def status_callback(is_connected: bool) -> None:
        async def update_state() -> None:
            async with state_lock:
                if not is_connected:
                    runtime_state["ws_reconnects"] += 1
                runtime_state["ws_connected"] = is_connected
        asyncio.create_task(update_state())

    app.state.ws_task = asyncio.create_task(
        gmo.websocket_loop(
            settings.api_key,
            settings.api_secret,
            settings.symbol,
            position_callback,
            execution_callback,
            status_callback,
            logger=logger,
        )
    )


@app.on_event("shutdown")
async def on_shutdown() -> None:
    if hasattr(app.state, "ws_task"):
        app.state.ws_task.cancel()
        with contextlib.suppress(Exception):
            await app.state.ws_task
    if hasattr(app.state, "rest_client"):
        await app.state.rest_client.aclose()
    if hasattr(app.state, "http_client"):
        await app.state.http_client.aclose()


@app.post("/webhook")
async def webhook_endpoint(request: Request) -> JSONResponse:
    try:
        data = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="invalid json") from exc

    try:
        base = BaseWebhook.parse_obj(data)
        payload: BaseWebhook
        if base.mode == "ENTRY":
            payload = EntryWebhook.parse_obj(data)
        else:
            payload = CloseWebhook.parse_obj(data)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors()) from exc

    await check_auth(payload)
    await check_freshness(payload)
    try:
        await ensure_idempotent(payload.event_id)
    except HTTPException as exc:
        if exc.status_code == 200:
            return JSONResponse({"status": "duplicate"})
        raise

    if payload.mode == "ENTRY":
        return await process_entry(payload)  # type: ignore[arg-type]
    return await process_close(payload)  # type: ignore[arg-type]


@app.get("/healthz")
async def healthz() -> JSONResponse:
    try:
        await asyncio.to_thread(storage.prune_old_events, settings.idempotency_ttl)
    except Exception as exc:
        raise HTTPException(status_code=500, detail="db error") from exc
    return JSONResponse({"status": "ok"})


@app.get("/status")
async def status_endpoint() -> JSONResponse:
    async with state_lock:
        snapshot = {
            "position_qty": float(runtime_state.get("position_qty", Decimal("0"))),
            "position_side": runtime_state.get("position_side"),
            "last_event_id": runtime_state.get("last_event_id"),
            "last_event_ts": runtime_state.get("last_event_ts"),
            "events_processed": runtime_state.get("events_processed"),
            "retry_stats": runtime_state.get("retry_stats"),
            "ws_connected": runtime_state.get("ws_connected"),
            "ws_reconnects": runtime_state.get("ws_reconnects"),
            "retries_total": runtime_state.get("retries_total"),
        }
    return JSONResponse(snapshot)
