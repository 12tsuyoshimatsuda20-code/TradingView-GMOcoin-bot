from __future__ import annotations

import asyncio
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:  # pragma: no cover - optional optimization
    import uvloop

    uvloop.install()
except Exception:  # pragma: no cover - optional
    pass

from fastapi import Depends, FastAPI, HTTPException, Request
from loguru import logger

from .gmo import GMOBroker, OrderResult
from .models import (
    HealthResponse,
    LastEvent,
    Mode,
    PositionSummary,
    RetryStats,
    StatusResponse,
    WebhookRequest,
    WebhookResponse,
)
from .notify import DiscordNotifier
from .storage import IdempotencyStorage


IDEMPOTENCY_TTL_SECONDS = 600


def configure_logging(log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    logger.remove()
    logger.add(sys.stdout, level="INFO", serialize=True)
    logger.add(
        log_dir / "app.log",
        rotation="00:00",
        retention="7 days",
        enqueue=True,
        serialize=True,
        level="INFO",
    )


@dataclass
class Settings:
    webhook_token: str
    gmo_api_key: str
    gmo_api_secret: str
    environment: str
    allowed_symbols: List[str]
    entry_policy: str
    max_skew_seconds: int
    qty_step: Decimal
    discord_webhook_url: Optional[str]
    timezone: str
    storage_path: Path
    version: Optional[str]

    @classmethod
    def load(cls) -> "Settings":
        env = os.getenv("ENV", "prod")
        allowed_symbols = [s.strip() for s in os.getenv("ALLOWED_SYMBOLS", "BTC_JPY").split(",") if s.strip()]
        qty_step = Decimal(os.getenv("QTY_STEP", "0.01"))
        timezone_name = os.getenv("TZ", "Asia/Tokyo")
        version = os.getenv("APP_VERSION") or get_git_revision()
        storage_path = Path(os.getenv("IDEMPOTENCY_DB", "/app/data/idempotency.db"))
        settings = cls(
            webhook_token=os.getenv("WEBHOOK_TOKEN", ""),
            gmo_api_key=os.getenv("GMO_API_KEY", ""),
            gmo_api_secret=os.getenv("GMO_API_SECRET", ""),
            environment=env,
            allowed_symbols=allowed_symbols,
            entry_policy=os.getenv("ENTRY_POLICY", "ignore"),
            max_skew_seconds=int(os.getenv("MAX_SKEW_SECONDS", "60")),
            qty_step=qty_step,
            discord_webhook_url=os.getenv("DISCORD_WEBHOOK_URL"),
            timezone=timezone_name,
            storage_path=storage_path,
            version=version,
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        if not self.webhook_token:
            raise RuntimeError("WEBHOOK_TOKEN must be set")
        if len(self.gmo_api_key) != 32:
            raise RuntimeError("GMO_API_KEY must be 32 characters long")
        if len(self.gmo_api_secret) != 64:
            raise RuntimeError("GMO_API_SECRET must be 64 characters long")
        if self.entry_policy not in {"ignore"}:
            raise RuntimeError("Unsupported ENTRY_POLICY")
        if not self.allowed_symbols:
            raise RuntimeError("ALLOWED_SYMBOLS must not be empty")
        if self.max_skew_seconds <= 0:
            raise RuntimeError("MAX_SKEW_SECONDS must be positive")
        if self.qty_step <= 0:
            raise RuntimeError("QTY_STEP must be positive")


@dataclass
class AppState:
    settings: Settings
    storage: IdempotencyStorage
    notifier: DiscordNotifier
    broker: GMOBroker
    last_event: LastEvent

    def update_last_event(self, event_id: str, mode: Mode, ts: datetime, status: str, detail: str) -> None:
        self.last_event = LastEvent(
            event_id=event_id,
            mode=mode,
            ts=ts,
            status=status,
            detail=detail,
        )


def get_git_revision() -> Optional[str]:  # pragma: no cover - runtime info only
    try:
        import subprocess

        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        return result.stdout.strip() or None
    except Exception:
        return None


app = FastAPI(title="exec-lane")


def get_state(request: Request) -> AppState:
    return request.app.state.app_state


def parse_timestamp(value: str) -> datetime:
    normalized = value.replace("Z", "+00:00")
    try:
        ts = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid ts format") from exc
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def floor_to_step(size: float, step: Decimal) -> float:
    decimal_size = Decimal(str(size))
    units = (decimal_size / step).to_integral_value(rounding=ROUND_DOWN)
    floored = units * step
    return float(floored)


def summarize_position(data: Dict[str, Any], symbol: str) -> Tuple[Optional[str], float]:
    entries = data.get("data") or []
    total = Decimal("0")
    side: Optional[str] = None
    for entry in entries:
        if entry.get("symbol") != symbol:
            continue
        try:
            size = Decimal(str(entry.get("size", "0")))
        except Exception:
            size = Decimal("0")
        if size > 0:
            side = entry.get("side")
        total += size
    return side, float(total)


def build_discord_embed(
    req: WebhookRequest, result: OrderResult, latency_ms: float, status: str, detail: str
) -> List[dict]:
    fields = [
        {"name": "Symbol", "value": req.symbol, "inline": True},
        {"name": "Mode", "value": req.mode.value, "inline": True},
        {"name": "Latency(ms)", "value": f"{latency_ms:.0f}", "inline": True},
    ]
    if req.mode == Mode.ENTRY and req.size is not None:
        fields.append({"name": "Size", "value": f"{req.size:.4f}", "inline": True})
        if req.side:
            fields.append({"name": "Side", "value": req.side, "inline": True})
    if result.message_code:
        fields.append({"name": "Message Code", "value": result.message_code, "inline": True})
    if result.message_string:
        fields.append({"name": "Message", "value": result.message_string, "inline": False})
    return [
        {
            "title": f"{status}",
            "description": detail,
            "fields": fields,
            "timestamp": datetime.utcnow().isoformat() + "Z",
        }
    ]


@app.on_event("startup")
async def on_startup() -> None:
    settings = Settings.load()
    configure_logging(Path("logs"))
    os.environ["TZ"] = settings.timezone
    try:
        time.tzset()
    except AttributeError:
        pass
    storage = IdempotencyStorage(settings.storage_path, IDEMPOTENCY_TTL_SECONDS)
    await storage.initialize()
    notifier = DiscordNotifier(settings.discord_webhook_url)
    await notifier.start()
    broker = GMOBroker(settings.gmo_api_key, settings.gmo_api_secret)
    await broker.connect()
    try:
        await broker.fetch_positions(settings.allowed_symbols[0])
    except Exception as exc:
        logger.warning("Initial position fetch failed", error=str(exc))
    app.state.app_state = AppState(
        settings=settings,
        storage=storage,
        notifier=notifier,
        broker=broker,
        last_event=LastEvent(event_id=None, mode=None, ts=None, status=None, detail=None),
    )
    logger.info("Application startup complete", environment=settings.environment)


@app.on_event("shutdown")
async def on_shutdown() -> None:
    state: AppState = app.state.app_state
    await state.notifier.close()
    await state.storage.close()
    await state.broker.close()
    logger.info("Application shutdown complete")


@app.get("/healthz", response_model=HealthResponse)
async def healthz() -> HealthResponse:
    return HealthResponse()


@app.get("/status", response_model=StatusResponse)
async def status(state: AppState = Depends(get_state)) -> StatusResponse:
    symbol = state.settings.allowed_symbols[0]
    summary = state.broker.position_summary.get(symbol, {"data": []})
    side, size = summarize_position(summary, symbol)
    retry_stats = RetryStats(
        entry_retries=state.broker.retry_entry,
        close_retries=state.broker.retry_close,
    )
    return StatusResponse(
        environment=state.settings.environment,
        last_event=state.last_event,
        position=PositionSummary(symbol=symbol, side=side, size=size),
        ws_connected=state.broker.ws_connected,
        retry_stats=retry_stats,
        version=state.settings.version,
    )


@app.post("/webhook", response_model=WebhookResponse)
async def webhook(payload: WebhookRequest, state: AppState = Depends(get_state)) -> WebhookResponse:
    settings = state.settings
    if payload.token != settings.webhook_token:
        raise HTTPException(status_code=401, detail="Invalid token")
    if payload.symbol not in settings.allowed_symbols:
        raise HTTPException(status_code=400, detail="Unsupported symbol")

    event_ts = parse_timestamp(payload.ts)
    now = datetime.now(timezone.utc)
    skew = abs((now - event_ts).total_seconds())
    if skew > settings.max_skew_seconds:
        raise HTTPException(status_code=400, detail="Timestamp skew exceeded")

    is_new = await state.storage.register(payload.event_id)
    log = logger.bind(event_id=payload.event_id, mode=payload.mode.value, symbol=payload.symbol)
    if not is_new:
        log.info("Duplicate event ignored")
        return WebhookResponse(status="duplicate", detail="Event already processed", event_id=payload.event_id)

    latency_ms = (now - event_ts).total_seconds() * 1000

    if payload.mode == Mode.ENTRY:
        return await handle_entry(payload, state, latency_ms, log, event_ts)
    return await handle_close(payload, state, latency_ms, log, event_ts)


async def handle_entry(
    payload: WebhookRequest,
    state: AppState,
    latency_ms: float,
    log,
    event_ts: datetime,
) -> WebhookResponse:
    settings = state.settings
    broker = state.broker

    summary = await broker.fetch_positions(payload.symbol)
    side, open_size = summarize_position(summary, payload.symbol)
    if open_size > 0:
        log.info("Existing position detected, entry ignored", position_side=side, position_size=open_size)
        state.update_last_event(payload.event_id, payload.mode, event_ts, "ENTRY IGNORED", "Position already open")
        return WebhookResponse(status="ignored", detail="Position already open", event_id=payload.event_id)

    floored_size = floor_to_step(payload.size or 0.0, settings.qty_step)
    if floored_size <= 0:
        raise HTTPException(status_code=400, detail="Size below minimum step")

    result = await broker.place_entry(payload.symbol, payload.side or "BUY", floored_size)
    status = "ENTRY OK" if result.success else "ENTRY ERROR"
    detail = "Order executed" if result.success else "Order failed"
    state.update_last_event(payload.event_id, payload.mode, event_ts, status, detail)

    log.bind(size=floored_size, side=payload.side).info(
        "ENTRY processed",
        latency_ms=latency_ms,
        result=status,
        message_code=result.message_code,
    )

    if result.success:
        order_id = result.data.get("data", {}).get("orderId") or result.data.get("orderId")
        fill = None
        if order_id:
            fill = await broker.wait_for_execution(order_id)
        if fill is None:
            try:
                await broker.fetch_positions(payload.symbol)
            except Exception as exc:
                log.warning("Post-entry position check failed", error=str(exc))
    embeds = build_discord_embed(payload, result, latency_ms, status, detail)
    await state.notifier.send(f"[{status}] {payload.symbol}", embeds=embeds)
    return WebhookResponse(status=status, detail=detail, event_id=payload.event_id)


async def handle_close(
    payload: WebhookRequest,
    state: AppState,
    latency_ms: float,
    log,
    event_ts: datetime,
) -> WebhookResponse:
    settings = state.settings
    broker = state.broker

    summary = await broker.fetch_positions(payload.symbol)
    side, open_size = summarize_position(summary, payload.symbol)
    if open_size <= 0:
        log.info("No open position to close")
        state.update_last_event(payload.event_id, payload.mode, event_ts, "CLOSE OK", "No position")
        return WebhookResponse(status="CLOSE OK", detail="No open position", event_id=payload.event_id)

    close_side = "SELL" if side == "BUY" else "BUY"
    result = await broker.close_bulk(payload.symbol)
    if not result.success:
        log.warning("closeBulkOrder failed, falling back to manual close", message_code=result.message_code)
        step_dec = settings.qty_step
        remaining_dec = (Decimal(str(open_size)) / step_dec).to_integral_value(rounding=ROUND_DOWN) * step_dec
        attempt_result: Optional[OrderResult] = None
        while remaining_dec > 0:
            remaining = float(remaining_dec)
            attempt_result = await broker.place_close(payload.symbol, close_side, remaining)
            if attempt_result.success:
                result = attempt_result
                break
            if attempt_result.status_code == 400 and remaining_dec > step_dec:
                remaining_dec -= step_dec
                continue
            else:
                result = attempt_result
                break
        if not result.success and attempt_result is not None:
            result = attempt_result
    status = "CLOSE OK" if result.success else "CLOSE ERROR"
    detail = "Position closed" if result.success else "Close failed"
    state.update_last_event(payload.event_id, payload.mode, event_ts, status, detail)
    log.info(
        "CLOSE processed",
        latency_ms=latency_ms,
        position_side=side,
        position_size=open_size,
        result=status,
        message_code=result.message_code,
    )
    if result.success:
        try:
            await broker.fetch_positions(payload.symbol)
        except Exception as exc:
            log.warning("Post-close position check failed", error=str(exc))
    embeds = build_discord_embed(payload, result, latency_ms, status, detail)
    await state.notifier.send(f"[{status}] {payload.symbol}", embeds=embeds)
    return WebhookResponse(status=status, detail=detail, event_id=payload.event_id)

