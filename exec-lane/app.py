from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, Optional

from cachetools import TTLCache
from fastapi import FastAPI, HTTPException, status
from loguru import logger
from pydantic import BaseModel, Field, root_validator, validator

from gmo_client import GMOCoinClient
from notify import post_discord
from orders import EntryIgnored, OrderExecutionError, OrderService, QuantityStepError
from settings import settings


def configure_logging() -> None:
    logger.remove()
    logger.add(sys.stdout, level=settings.log_level)
    logger.add(
        settings.log_directory / "app.log",
        rotation=settings.log_rotation,
        retention=settings.log_retention,
        enqueue=True,
        level=settings.log_level,
    )


configure_logging()

app = FastAPI(title="TradingView GMOcoin Bot")

gmo_client = GMOCoinClient(settings.gmo_api_key, settings.gmo_api_secret)
order_service = OrderService(gmo_client)

event_cache: TTLCache[str, datetime] = TTLCache(
    maxsize=1024, ttl=settings.event_id_ttl_seconds
)
event_cache_lock = asyncio.Lock()


class WebhookPayload(BaseModel):
    token: str
    event_id: str = Field(..., min_length=1)
    ts: datetime
    symbol: str
    size: Decimal
    mode: str
    side: Optional[str] = None
    entry_price_hint: Optional[Decimal] = None
    tp1_price: Optional[Decimal] = None

    @validator("ts", pre=True)
    def parse_ts(cls, value: Any) -> datetime:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    @validator("mode", pre=True)
    def uppercase_mode(cls, value: str) -> str:
        return value.upper()

    @validator("mode")
    def validate_mode(cls, value: str) -> str:
        if value not in {"ENTRY", "CLOSE"}:
            raise ValueError("mode must be ENTRY or CLOSE")
        return value

    @validator("side", pre=True)
    def uppercase_side(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        return value.upper()

    @validator("side")
    def validate_side(cls, value: Optional[str], values: Dict[str, Any]) -> Optional[str]:
        if value is None:
            return value
        if value not in {"BUY", "SELL"}:
            raise ValueError("side must be BUY or SELL")
        return value

    @validator("symbol")
    def validate_symbol(cls, value: str) -> str:
        if value != settings.symbol:
            raise ValueError("Unsupported symbol")
        return value

    @validator("size", pre=True)
    def to_decimal(cls, value: Any) -> Decimal:
        return Decimal(str(value))

    @root_validator
    def check_side_for_entry(cls, values: Dict[str, Any]) -> Dict[str, Any]:
        mode = values.get("mode")
        side = values.get("side")
        if mode == "ENTRY" and side not in {"BUY", "SELL"}:
            raise ValueError("side is required for ENTRY mode")
        return values


@app.on_event("shutdown")
async def shutdown_event() -> None:
    await gmo_client.close()


@app.get("/healthz")
async def healthz() -> Dict[str, str]:
    return {"status": "ok"}


@app.get("/status")
async def status_endpoint() -> Dict[str, Any]:
    positions = await gmo_client.get_open_positions(settings.symbol)
    return {
        "symbol": settings.symbol,
        "positions": positions.get("data", []),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.post("/webhook")
async def webhook(payload: WebhookPayload) -> Dict[str, Any]:
    if payload.token != settings.webhook_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")

    skew = abs((datetime.now(timezone.utc) - payload.ts).total_seconds())
    if skew > settings.max_skew_seconds:
        await post_discord(
            "WARN",
            "Payload rejected",
            f"Timestamp skew too large ({skew:.2f}s)",
            {"event_id": payload.event_id},
        )
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Timestamp skew too large")

    async with event_cache_lock:
        if payload.event_id in event_cache:
            logger.info("Duplicate event received", event_id=payload.event_id)
            return {"ok": True, "duplicate": True, "event_id": payload.event_id}
        event_cache[payload.event_id] = datetime.now(timezone.utc)

    try:
        if payload.mode == "ENTRY":
            try:
                result = await order_service.entry_if_flat(payload.side, payload.size)
            except EntryIgnored:
                logger.info("ENTRY ignored", event_id=payload.event_id)
                return {"ok": True, "ignored": True, "event_id": payload.event_id}

            await post_discord(
                "INFO",
                "ENTRY executed",
                f"{payload.side} {payload.size} {payload.symbol}",
                {
                    "event_id": payload.event_id,
                    "response": result,
                },
            )
            return {
                "ok": True,
                "mode": "ENTRY",
                "detail": "Market entry order submitted",
                "event_id": payload.event_id,
            }

        result = await order_service.close_all()
        detail = "Market close submitted"
        if result.get("status") == "already_flat":
            detail = "Position already flat"
        await post_discord(
            "INFO",
            "CLOSE processed",
            detail,
            {
                "event_id": payload.event_id,
                "response": result,
            },
        )
        return {
            "ok": True,
            "mode": "CLOSE",
            "detail": detail,
            "event_id": payload.event_id,
        }
    except QuantityStepError as exc:
        await post_discord(
            "WARN",
            "Invalid quantity",
            str(exc),
            {"event_id": payload.event_id},
        )
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    except OrderExecutionError as exc:
        await post_discord(
            "ERROR",
            "Order execution failed",
            str(exc),
            {"event_id": payload.event_id},
        )
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Order execution failed")
    except Exception as exc:
        logger.exception("Unhandled webhook error", event_id=payload.event_id)
        await post_discord(
            "ERROR",
            "Unhandled error",
            str(exc),
            {"event_id": payload.event_id},
        )
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal server error")
