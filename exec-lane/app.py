from __future__ import annotations

import os
import sys
import time
import contextlib
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from typing import Any, Dict

import aiohttp
import pybotters
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from loguru import logger
from pydantic import BaseSettings, Field, validator

from .gmo_client import GMOCoinClient
from .notify import DiscordNotifier
from .store import IdempotencyStore, StatusStore
from .validators import ClosePayload, EntryPayload


def configure_logging() -> None:
    logger.remove()
    logger.add(sys.stdout, serialize=True, level=os.getenv("LOG_LEVEL", "INFO"))


class Settings(BaseSettings):
    webhook_token: str = Field(..., env="WEBHOOK_TOKEN")
    gmo_api_key: str = Field(..., env="GMO_API_KEY")
    gmo_api_secret: str = Field(..., env="GMO_API_SECRET")
    allowed_symbols: str = Field("BTC_JPY", env="ALLOWED_SYMBOLS")
    entry_policy: str = Field("ignore", env="ENTRY_POLICY")
    max_skew_seconds: int = Field(60, env="MAX_SKEW_SECONDS")
    qty_step: Decimal = Field(Decimal("0.01"), env="QTY_STEP")
    notify_discord_webhook_url: str | None = Field(None, env="NOTIFY_DISCORD_WEBHOOK_URL")
    env: str = Field("prod", env="ENV")

    class Config:
        case_sensitive = False

    @validator("qty_step", pre=True)
    def validate_qty_step(cls, value: Any) -> Decimal:
        return Decimal(str(value))

    @validator("entry_policy")
    def validate_policy(cls, value: str) -> str:
        if value not in {"ignore"}:
            raise ValueError("ENTRY_POLICY must be 'ignore'")
        return value

    @validator("allowed_symbols")
    def validate_symbols(cls, value: str) -> str:
        if value.strip() != "BTC_JPY":
            raise ValueError("ALLOWED_SYMBOLS must contain BTC_JPY only")
        return value


def load_settings() -> Settings:
    env_path = Path("/app/config/.env")
    if env_path.exists():
        load_dotenv(env_path)
    return Settings()


def truncate_size(size: float, step: Decimal) -> Decimal:
    quantized = (Decimal(str(size)) / step).to_integral_value(rounding=ROUND_DOWN)
    return (quantized * step).quantize(step)


configure_logging()
settings = load_settings()
app = FastAPI(title="TradingView-GMOcoin Bridge", version="1.0.0")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(
        "Starting application",
        extra={
            "env": settings.env,
            "api_key_length": len(settings.gmo_api_key),
            "api_secret_length": len(settings.gmo_api_secret),
        },
    )

    http_timeout = aiohttp.ClientTimeout(total=15)
    # Notifier用の aiohttp セッション（Discord専用）
    notifier_session = aiohttp.ClientSession(timeout=http_timeout)
    status_store = StatusStore()
    notifier = DiscordNotifier(notifier_session, settings.notify_discord_webhook_url)
    idempotency = IdempotencyStore(Path("/app/logs/runtime.db"))

    # pybotters は apis を (API_KEY, API_SECRET) のタプルで渡す
    pyb_client = pybotters.Client(
        apis={"gmocoin": (settings.gmo_api_key, settings.gmo_api_secret)}
    )
    gmo_client = GMOCoinClient(pyb_client, status_store)

    app.state.notifier_session = notifier_session
    app.state.status_store = status_store
    app.state.notifier = notifier
    app.state.idempotency = idempotency
    app.state.gmo_client = gmo_client
    app.state.settings = settings

    try:
        yield
    finally:
        with contextlib.suppress(Exception):
            await pyb_client.close()
        with contextlib.suppress(Exception):
            await notifier_session.close()


app.router.lifespan_context = lifespan


async def get_settings() -> Settings:
    return settings


@app.get("/healthz")
async def healthz() -> Dict[str, str]:
    return {"status": "ok"}


@app.get("/status")
async def status(request: Request):
    status_store: StatusStore = request.app.state.status_store
    snapshot = await status_store.snapshot()
    return JSONResponse(
        {
            "position_qty": snapshot.position_qty,
            "position_side": snapshot.position_side,
            "last_event_id": snapshot.last_event_id,
            "last_event_ts": snapshot.last_event_ts,
            "retry_stats": snapshot.retry_stats,
            "ws_connected": snapshot.ws_connected,
        }
    )


async def ensure_token(token: str, settings: Settings) -> None:
    if token != settings.webhook_token:
        logger.warning("Invalid webhook token", extra={"token_length": len(token)})
        raise HTTPException(status_code=401, detail="unauthorized")


def ensure_fresh(ts: datetime, settings: Settings) -> None:
    now = datetime.now(timezone.utc)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    skew = abs((now - ts).total_seconds())
    if skew > settings.max_skew_seconds:
        raise HTTPException(status_code=400, detail="timestamp skew too large")


async def process_entry(payload: EntryPayload, request: Request) -> Dict[str, Any]:
    gmo_client: GMOCoinClient = request.app.state.gmo_client
    status_store: StatusStore = request.app.state.status_store
    notifier: DiscordNotifier = request.app.state.notifier

    current = await gmo_client.fetch_position_summary(payload.symbol)
    if current.size > 0:
        logger.info(
            "ENTRY ignored due to existing position",
            extra={"event_id": payload.event_id, "mode": payload.mode, "symbol": payload.symbol},
        )
        return {"ignored": True, "reason": "position_exists"}

    qty = truncate_size(payload.size, settings.qty_step)
    if qty <= 0:
        raise HTTPException(status_code=400, detail="size below minimum step")

    started = time.perf_counter()
    try:
        order_result = await gmo_client.place_market_order(
            symbol=payload.symbol,
            side=payload.side,
            size=qty,
        )
    except Exception as exc:
        await notifier.notify_error(event_id=payload.event_id, message=str(exc))
        raise HTTPException(status_code=500, detail="order_failed") from exc

    summary = await gmo_client.wait_for_position(
        symbol=payload.symbol,
        expected_side=payload.side,
        expected_size=float(qty),
    )
    latency_ms = (time.perf_counter() - started) * 1000
    await status_store.set_last_event(payload.event_id, payload.ts.isoformat())
    await notifier.notify_entry_ok(
        event_id=payload.event_id,
        side=payload.side,
        size=float(qty),
        price=summary.average_price,
        latency_ms=latency_ms,
    )
    logger.info(
        "ENTRY executed",
        extra={
            "event_id": payload.event_id,
            "mode": payload.mode,
            "symbol": payload.symbol,
            "side": payload.side,
            "size": float(qty),
            "order_id": order_result.order_id,
            "latency_ms": latency_ms,
        },
    )
    return {
        "order_id": order_result.order_id,
        "filled_qty": float(summary.size),
        "latency_ms": latency_ms,
    }


async def process_close(payload: ClosePayload, request: Request) -> Dict[str, Any]:
    gmo_client: GMOCoinClient = request.app.state.gmo_client
    status_store: StatusStore = request.app.state.status_store
    notifier: DiscordNotifier = request.app.state.notifier

    current = await gmo_client.fetch_position_summary(payload.symbol)
    if current.size == 0:
        logger.info(
            "CLOSE ignored because already flat",
            extra={"event_id": payload.event_id, "mode": payload.mode},
        )
        return {"already_flat": True}

    close_side = "SELL" if current.side == "BUY" else "BUY"
    total_size, settable_size, settle_payload = await gmo_client.fetch_settlement_size(
        payload.symbol
    )
    if not settle_payload:
        logger.info(
            "CLOSE ignored because no open positions returned",
            extra={"event_id": payload.event_id},
        )
        return {"already_flat": True}

    qty = total_size if total_size > 0 else current.size

    def shrink_settle_positions(
        entries: list[dict[str, str]], target: float
    ) -> list[dict[str, str]]:
        remaining = Decimal(str(target))
        shrunk: list[dict[str, str]] = []
        for item in entries:
            if remaining <= 0:
                break
            size_dec = Decimal(item["size"])
            use = size_dec if size_dec <= remaining else remaining
            quantized = use.quantize(settings.qty_step, rounding=ROUND_DOWN)
            if quantized <= 0:
                continue
            shrunk.append(
                {
                    "positionId": item["positionId"],
                    "size": format(quantized, ".2f"),
                }
            )
            remaining -= quantized
        return shrunk

    started = time.perf_counter()
    try:
        await gmo_client.place_market_order(
            symbol=payload.symbol,
            side=close_side,
            settle_position=settle_payload,
        )
    except RuntimeError as exc:
        if "ERR-200" in str(exc) and settable_size < total_size and settable_size > 0:
            logger.warning(
                "Settlement limited, retrying with settable size",
                extra={"event_id": payload.event_id, "settable": settable_size},
            )
            trimmed = shrink_settle_positions(settle_payload, settable_size)
            if not trimmed:
                await notifier.notify_error(event_id=payload.event_id, message="no settle qty available")
                raise HTTPException(status_code=400, detail="no_settle_quantity")
            await gmo_client.place_market_order(
                symbol=payload.symbol,
                side=close_side,
                settle_position=trimmed,
            )
            qty = settable_size
            settle_payload = trimmed
        else:
            await notifier.notify_error(event_id=payload.event_id, message=str(exc))
            raise HTTPException(status_code=500, detail="close_failed") from exc

    summary = await gmo_client.wait_for_position(
        symbol=payload.symbol,
        expected_side="FLAT",
        expected_size=0.0,
    )
    latency_ms = (time.perf_counter() - started) * 1000
    await status_store.set_last_event(payload.event_id, payload.ts.isoformat())
    await notifier.notify_close_ok(
        event_id=payload.event_id,
        closed_side=current.side,
        closed_qty=qty,
        pnl=None,
        latency_ms=latency_ms,
    )
    logger.info(
        "CLOSE executed",
        extra={
            "event_id": payload.event_id,
            "mode": payload.mode,
            "symbol": payload.symbol,
            "closed_side": close_side,
            "closed_qty": qty,
            "latency_ms": latency_ms,
        },
    )
    return {
        "closed_qty": qty,
        "latency_ms": latency_ms,
        "position_flat": summary.size == 0,
    }


@app.post("/webhook")
async def webhook(request: Request, settings: Settings = Depends(get_settings)):
    payload_dict = await request.json()
    mode = payload_dict.get("mode")
    if mode == "ENTRY":
        payload = EntryPayload(**payload_dict)
    elif mode == "CLOSE":
        payload = ClosePayload(**payload_dict)
    else:
        raise HTTPException(status_code=400, detail="mode must be ENTRY or CLOSE")

    await ensure_token(payload.token, settings)
    ensure_fresh(payload.ts, settings)

    idempotency: IdempotencyStore = request.app.state.idempotency
    registered = await idempotency.register_event(payload.event_id, payload.ts.isoformat())
    if not registered:
        logger.info("Duplicate event", extra={"event_id": payload.event_id})
        return {"duplicate": True}

    try:
        if mode == "ENTRY":
            response = await process_entry(payload, request)
        else:
            response = await process_close(payload, request)
        return response
    except HTTPException:
        await idempotency.remove_event(payload.event_id)
        raise
    except Exception as exc:
        await idempotency.remove_event(payload.event_id)
        notifier: DiscordNotifier = request.app.state.notifier
        await notifier.notify_error(event_id=payload.event_id, message=str(exc))
        logger.exception("Webhook processing failed", extra={"event_id": payload.event_id})
        raise HTTPException(status_code=500, detail="internal_error")


if os.name == "posix":  # uvloop on Linux
    try:
        import uvloop

        uvloop.install()
    except Exception:  # pragma: no cover - optional dependency errors
        logger.warning("uvloop install failed", exc_info=True)
