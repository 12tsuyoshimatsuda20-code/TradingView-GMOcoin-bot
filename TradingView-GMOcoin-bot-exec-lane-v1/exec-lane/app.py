"""FastAPI entrypoint for TradingView -> GMO Coin execution lane."""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List

import uvloop
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from loguru import logger
import pybotters

from models import BotState, Settings, WebhookPayload
from notify import Notifier
import gmocoin as gmo

uvloop.install()

load_dotenv("config/.env")

LOG_DIR = Path("/logs")
LOG_DIR.mkdir(parents=True, exist_ok=True)

logger.remove()
logger.add(sys.stdout, format="{time:YYYY-MM-DDTHH:mm:ss.SSSZ} | {level} | {message}", level="INFO")
logger.add(
    LOG_DIR / "exec-lane.log",
    level="INFO",
    rotation="10 MB",
    retention=10,
    enqueue=True,
    serialize=True,
)

SETTINGS = Settings(
    webhook_token=os.getenv("WEBHOOK_TOKEN", ""),
    allowed_symbols=os.getenv("ALLOWED_SYMBOLS", "BTC_JPY"),
    entry_policy=os.getenv("ENTRY_POLICY", "ignore"),
    max_skew_seconds=int(os.getenv("MAX_SKEW_SECONDS", "60")),
    qty_step=float(os.getenv("QTY_STEP", "0.01")),
    env=os.getenv("ENV", "prod"),
    discord_webhook_url=os.getenv("NOTIFY_DISCORD_WEBHOOK_URL"),
)

APIS = {
    "gmocoin": {
        "apiKey": os.getenv("GMO_API_KEY", ""),
        "secret": os.getenv("GMO_API_SECRET", ""),
    }
}

STATE = BotState()
notifier = Notifier(SETTINGS.discord_webhook_url)

_idempotency: Dict[str, float] = {}
_IDEMPOTENCY_TTL = 600.0
_idempotency_lock = asyncio.Lock()

app = FastAPI()


def _allowed_symbols() -> List[str]:
    return [symbol.strip() for symbol in SETTINGS.allowed_symbols.split(",") if symbol.strip()]


async def _check_idempotency(event_id: str) -> bool:
    now_ts = datetime.now(timezone.utc).timestamp()
    async with _idempotency_lock:
        # purge expired
        for key, stored_ts in list(_idempotency.items()):
            if now_ts - stored_ts > _IDEMPOTENCY_TTL:
                _idempotency.pop(key, None)
        if event_id in _idempotency:
            return False
        _idempotency[event_id] = now_ts
        return True


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.get("/status")
async def status():
    STATE.retry_stats = dict(gmo.RETRY_METRICS)
    return {
        "position_qty": STATE.position_cache.get("qty"),
        "position_side": STATE.position_cache.get("side"),
        "last_event_id": STATE.last_event_id,
        "last_event_ts": STATE.last_event_ts,
        "retry_stats": STATE.retry_stats,
        "ws_connected": STATE.ws_connected,
    }


def _parse_ts(ts_str: str) -> datetime:
    candidate = ts_str.replace("Z", "+00:00")
    return datetime.fromisoformat(candidate)


@app.post("/webhook")
async def webhook(payload: WebhookPayload, request: Request):
    event_logger = logger.bind(
        event_id=payload.event_id,
        mode=payload.mode,
        symbol=payload.symbol,
        side=payload.side,
        size=payload.size,
    )
    received_at = datetime.now(timezone.utc)
    event_logger.info("webhook received")

    if payload.token != SETTINGS.webhook_token:
        event_logger.warning("token mismatch")
        raise HTTPException(status_code=401, detail="token mismatch")

    is_new = await _check_idempotency(payload.event_id)
    if not is_new:
        event_logger.info("duplicate event ignored")
        return {"status": "ok", "detail": "duplicate event ignored"}

    try:
        event_ts = _parse_ts(payload.ts)
    except ValueError:
        event_logger.warning("invalid timestamp")
        raise HTTPException(status_code=400, detail="invalid ts")

    skew = abs((received_at - event_ts).total_seconds())
    if skew > SETTINGS.max_skew_seconds:
        event_logger.warning("stale timestamp skew={}s", skew)
        raise HTTPException(status_code=422, detail=f"stale ts ({int(skew)}s)")

    if payload.symbol not in _allowed_symbols():
        event_logger.warning("symbol not allowed")
        raise HTTPException(status_code=400, detail="symbol not allowed")

    STATE.last_event_id = payload.event_id
    STATE.last_event_ts = payload.ts

    try:
        async with pybotters.Client(apis=APIS["gmocoin"]) as client:
            position = await gmo.get_positions(client, payload.symbol)
            STATE.position_cache = position
            STATE.retry_stats = dict(gmo.RETRY_METRICS)

            if payload.mode == "ENTRY":
                if position.get("qty"):
                    event_logger.info("position exists; entry ignored")
                    return {"status": "ok", "detail": "position exists; entry ignored"}

                size = gmo.round_qty(float(payload.size), SETTINGS.qty_step)
                if size <= 0:
                    event_logger.warning("size too small after rounding")
                    raise HTTPException(status_code=400, detail="size too small after rounding")

                response = await gmo.place_entry_order(
                    client, payload.symbol, payload.side, size
                )
                STATE.position_cache = {"qty": size, "side": payload.side}
                latency = (datetime.now(timezone.utc) - received_at).total_seconds()
                event_logger.bind(result="entry_ok", latency=latency).info("entry order placed")
                await notifier.send_info(
                    "ENTRY EXECUTED",
                    f"event_id={payload.event_id} symbol={payload.symbol} side={payload.side} size={size}",
                )
                return {"status": "ok", "detail": response}

            if payload.mode == "CLOSE":
                qty = float(position.get("qty") or 0.0)
                if qty <= 0:
                    latency = (datetime.now(timezone.utc) - received_at).total_seconds()
                    event_logger.bind(result="already_flat", latency=latency).info("already flat")
                    await notifier.send_info(
                        "CLOSE SKIPPED",
                        f"event_id={payload.event_id} symbol={payload.symbol} detail=already_flat",
                    )
                    return {"status": "ok", "detail": "already flat"}

                close_side = "SELL" if position.get("side") == "BUY" else "BUY"
                remaining = qty
                attempts = 0
                while remaining > 0 and attempts < 5:
                    attempts += 1
                    try:
                        response = await gmo.place_close_order(
                            client, payload.symbol, close_side, remaining
                        )
                    except gmo.GmoAPIError as exc:
                        event_logger.bind(attempt=attempts).warning(
                            "close attempt failed status={} code={} msg={}",
                            exc.status_code,
                            exc.message_code,
                            exc.message_string,
                        )
                        if exc.is_settle_qty_error and remaining > SETTINGS.qty_step:
                            remaining = gmo.round_qty(remaining - SETTINGS.qty_step, SETTINGS.qty_step)
                            continue
                        raise
                    else:
                        STATE.position_cache = {"qty": 0.0, "side": None}
                        latency = (datetime.now(timezone.utc) - received_at).total_seconds()
                        event_logger.bind(result="close_ok", latency=latency).info(
                            "close order placed"
                        )
                        await notifier.send_info(
                            "CLOSE EXECUTED",
                            f"event_id={payload.event_id} symbol={payload.symbol} closed_qty={qty}",
                        )
                        return {"status": "ok", "detail": response}

                raise HTTPException(status_code=500, detail="close exhausted retries")

        raise HTTPException(status_code=400, detail="unknown mode")

    except HTTPException:
        raise
    except gmo.GmoAPIError as exc:
        latency = (datetime.now(timezone.utc) - received_at).total_seconds()
        event_logger.bind(result="gmo_error", latency=latency).error(str(exc))
        await notifier.send_error(
            "ORDER ERROR",
            (
                f"event_id={payload.event_id} symbol={payload.symbol} mode={payload.mode} "
                f"status={exc.status_code} code={exc.message_code} msg={exc.message_string}"
            ),
        )
        raise HTTPException(status_code=502, detail=str(exc))
    except Exception as exc:
        latency = (datetime.now(timezone.utc) - received_at).total_seconds()
        event_logger.bind(result="exception", latency=latency).exception("unexpected error")
        await notifier.send_error(
            "UNEXPECTED ERROR",
            f"event_id={payload.event_id} symbol={payload.symbol} mode={payload.mode} err={exc}",
        )
        raise HTTPException(status_code=500, detail=str(exc))
