from __future__ import annotations

import asyncio
import math
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse
from loguru import logger

from .domain import TradingService
from .infra.gmocoin_client import GMOCoinAPIError, GMOCoinClient
from .infra.positions import PositionsService
from .notify import notify_discord
from .schemas import (
    HealthResponse,
    LastEventInfo,
    PositionInfo,
    StatusResponse,
    WebhookPayload,
    WebhookResponse,
)
from .store import EventStore
from .version import VERSION


class WebhookError(Exception):
    def __init__(self, status_code: int, reason: str) -> None:
        self.status_code = status_code
        self.reason = reason
        super().__init__(reason)


@dataclass
class Settings:
    webhook_token: str
    gmo_api_key: str
    gmo_api_secret: str
    discord_webhook: Optional[str]
    symbol: str
    entry_policy: str
    max_skew_seconds: int
    qty_step: float
    timezone: str
    debug_signature: bool = False

    @classmethod
    def load(cls) -> "Settings":
        dotenv_path = Path("config/.env")
        if dotenv_path.exists():
            load_dotenv(dotenv_path)
        webhook_token = os.getenv("WEBHOOK_TOKEN")
        gmo_api_key = os.getenv("GMO_API_KEY")
        gmo_api_secret = os.getenv("GMO_API_SECRET")
        discord_webhook = os.getenv("DISCORD_WEBHOOK")
        symbol = os.getenv("SYMBOL", "BTC_JPY")
        entry_policy = os.getenv("ENTRY_POLICY", "ignore").lower()
        max_skew_seconds = int(os.getenv("MAX_SKEW_SECONDS", "60"))
        qty_step = float(os.getenv("QTY_STEP", "0.01"))
        timezone_value = os.getenv("TZ", "Asia/Tokyo")
        debug_signature = os.getenv("DEBUG_SIGNATURE", "false").lower() == "true"

        required = {
            "WEBHOOK_TOKEN": webhook_token,
            "GMO_API_KEY": gmo_api_key,
            "GMO_API_SECRET": gmo_api_secret,
        }
        missing = [key for key, value in required.items() if not value]
        if missing:
            raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")

        return cls(
            webhook_token=webhook_token,
            gmo_api_key=gmo_api_key,
            gmo_api_secret=gmo_api_secret,
            discord_webhook=discord_webhook,
            symbol=symbol,
            entry_policy=entry_policy,
            max_skew_seconds=max_skew_seconds,
            qty_step=qty_step,
            timezone=timezone_value,
            debug_signature=debug_signature,
        )


def configure_logging() -> None:
    logger.remove()
    logger.add(
        lambda msg: print(msg, end=""),
        level="INFO",
        format="{time:YYYY-MM-DDTHH:mm:ssZ} | {level} | {name}:{line} | {message}",
    )


def ensure_directories() -> None:
    Path("data").mkdir(parents=True, exist_ok=True)
    Path("logs").mkdir(parents=True, exist_ok=True)


class AppState:
    def __init__(self) -> None:
        self.settings: Settings
        self.store: EventStore
        self.gmo_client: GMOCoinClient
        self.positions: PositionsService
        self.trading: TradingService
        self.lock: asyncio.Lock


def create_app(
    settings: Optional[Settings] = None,
    *,
    store: Optional[EventStore] = None,
    gmocoin_client: Optional[GMOCoinClient] = None,
    positions_service: Optional[PositionsService] = None,
) -> FastAPI:
    configure_logging()
    ensure_directories()
    settings = settings or Settings.load()
    os.environ["TZ"] = settings.timezone
    try:
        time.tzset()
    except AttributeError:
        logger.warning("tzset not supported on this platform")

    app = FastAPI(title="TradingView GMO Coin Bot", version=VERSION)
    state = AppState()
    state.settings = settings
    state.store = store or EventStore(Path("data/bot.db"))
    state.gmo_client = gmocoin_client or GMOCoinClient(
        settings.gmo_api_key,
        settings.gmo_api_secret,
        debug_signature=settings.debug_signature,
    )
    state.positions = positions_service or PositionsService(state.gmo_client)
    state.trading = TradingService(
        symbol=settings.symbol,
        entry_policy=settings.entry_policy,
        positions_service=state.positions,
        gmocoin_client=state.gmo_client,
        discord_webhook=settings.discord_webhook,
    )
    state.lock = asyncio.Lock()
    app.state.app_state = state

    async def safe_notify(
        title: str,
        description: str,
        color: str = "gray",
        fields: list[dict] | None = None,
    ) -> None:
        try:
            await notify_discord(
                state.settings.discord_webhook,
                title,
                description,
                color,
                fields,
            )
        except Exception as exc:
            logger.debug("Discord notify suppressed (main): {}", repr(exc))

    @app.exception_handler(WebhookError)
    async def webhook_error_handler(request: Request, exc: WebhookError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code, content={"status": "error", "reason": exc.reason}
        )

    @app.on_event("startup")
    async def on_startup() -> None:  # pragma: no cover - startup hook
        await state.store.connect()
        logger.info("Application started with symbol=%s", state.settings.symbol)

    @app.on_event("shutdown")
    async def on_shutdown() -> None:  # pragma: no cover - shutdown hook
        await state.store.close()
        await state.gmo_client.close()
        logger.info("Application shutdown complete")

    @app.get("/healthz", response_model=HealthResponse)
    async def healthz() -> HealthResponse:
        return HealthResponse()

    @app.get("/status", response_model=StatusResponse)
    async def status_endpoint() -> StatusResponse:
        try:
            position = await state.positions.fetch_state(state.settings.symbol)
        except GMOCoinAPIError as exc:
            logger.error("Failed to fetch positions: %s", exc)
            raise HTTPException(status_code=502, detail="Failed to fetch positions")

        last_event = await state.store.fetch_last_event()
        last_event_info = (
            LastEventInfo(id=last_event.event_id, at=last_event.received_at, action=last_event.action)
            if last_event
            else LastEventInfo()
        )
        position_info = PositionInfo(side=position.side, size=position.size)
        return StatusResponse(
            position=position_info,
            last_event=last_event_info,
            version=VERSION,
        )

    def validate_payload(payload: WebhookPayload) -> None:
        if payload.token != state.settings.webhook_token:
            raise WebhookError(status.HTTP_400_BAD_REQUEST, "Invalid token")
        if payload.symbol != state.settings.symbol:
            raise WebhookError(status.HTTP_400_BAD_REQUEST, "Invalid symbol")
        if payload.size <= 0:
            raise WebhookError(status.HTTP_400_BAD_REQUEST, "Size must be positive")
        step_ratio = payload.size / state.settings.qty_step
        if not math.isclose(step_ratio, round(step_ratio), rel_tol=0, abs_tol=1e-9):
            raise WebhookError(
                status.HTTP_400_BAD_REQUEST,
                f"Size must align with step {state.settings.qty_step}",
            )
        ts = payload.ts
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        skew = abs((datetime.now(timezone.utc) - ts.astimezone(timezone.utc)).total_seconds())
        if skew > state.settings.max_skew_seconds:
            raise WebhookError(status.HTTP_400_BAD_REQUEST, "Timestamp skew too large")

    @app.post("/webhook", response_model=WebhookResponse)
    async def webhook_endpoint(request: Request, payload: WebhookPayload) -> JSONResponse:
        content_type = request.headers.get("content-type", "")
        if "application/json" not in content_type.lower():
            raise WebhookError(
                status.HTTP_400_BAD_REQUEST, "Content-Type must be application/json"
            )

        validate_payload(payload)
        inserted = await state.store.register_event(
            payload.event_id,
            payload.mode,
            payload.model_dump(mode="json"),
        )
        if not inserted:
            return JSONResponse(
                status_code=status.HTTP_200_OK,
                content=WebhookResponse(status="duplicate", event_id=payload.event_id).model_dump(),
            )

        async with state.lock:
            try:
                if payload.mode == "ENTRY":
                    result = await state.trading.process_entry(payload)
                else:
                    result = await state.trading.process_close(payload)
                await state.store.update_event(
                    payload.event_id,
                    status="completed",
                    action=result.action,
                    response=result.details,
                )
                response = WebhookResponse(
                    status="ok",
                    event_id=payload.event_id,
                    action=result.action,
                )
                return JSONResponse(status_code=status.HTTP_200_OK, content=response.model_dump())
            except GMOCoinAPIError as exc:
                message = f"GMO Coin API error: {exc}"
                await state.store.update_event(
                    payload.event_id,
                    status="failed",
                    action="error",
                    response={"status_code": exc.status_code, "payload": exc.payload},
                    error=message,
                )
                await safe_notify(
                    "Execution error",
                    f"event_id={payload.event_id}\\n{message}",
                    "red",
                    [
                        {"name": "status_code", "value": str(exc.status_code), "inline": True},
                    ],
                )
                return JSONResponse(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    content={"status": "error", "reason": message},
                )
            except Exception as exc:
                message = f"Unhandled error: {exc}"
                logger.exception("Unhandled error while processing webhook")
                await state.store.update_event(
                    payload.event_id,
                    status="failed",
                    action="error",
                    error=message,
                )
                await safe_notify(
                    "Execution error",
                    f"event_id={payload.event_id}\\n{message}",
                    "red",
                )
                return JSONResponse(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    content={"status": "error", "reason": "Internal server error"},
                )

    return app


app = create_app()
