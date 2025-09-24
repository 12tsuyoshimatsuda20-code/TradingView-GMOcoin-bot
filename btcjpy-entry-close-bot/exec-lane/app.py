from __future__ import annotations

import asyncio
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, Literal, Optional, Union

try:  # uvloop is optional on non-Linux platforms
    import uvloop  # type: ignore
except ImportError:  # pragma: no cover
    uvloop = None  # type: ignore
else:  # pragma: no cover
    uvloop.install()

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import ORJSONResponse
from loguru import logger
from pydantic import BaseModel, Field, TypeAdapter, ValidationError, field_validator

from gmo import GMOCoinClient, GMOCoinError
from notify import DiscordNotifier
from runtime import EventRecord, RuntimeState
from ws import GMOWebSocketSupervisor

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = BASE_DIR / "config"
LOG_DIR = BASE_DIR / "logs"
ENV_FILE = CONFIG_DIR / ".env"

load_dotenv(ENV_FILE, override=False)


def setup_logging(log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    logger.remove()
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    logger.add(sys.stdout, level=level, serialize=True, backtrace=False, diagnose=False)
    logger.add(log_dir / "runtime.log", level=level, serialize=True, rotation="7 days", retention=5)


class Settings(BaseModel):
    webhook_token: str = Field(alias="WEBHOOK_TOKEN")
    gmo_api_key: str = Field(alias="GMO_API_KEY")
    gmo_api_secret: str = Field(alias="GMO_API_SECRET")
    allowed_symbols: set[str] = Field(alias="ALLOWED_SYMBOLS")
    entry_policy: str = Field(alias="ENTRY_POLICY")
    max_skew_seconds: int = Field(alias="MAX_SKEW_SECONDS")
    qty_step: float = Field(alias="QTY_STEP")
    discord_webhook_url: Optional[str] = Field(default=None, alias="NOTIFY_DISCORD_WEBHOOK_URL")
    env: str = Field(default="prod", alias="ENV")

    @field_validator("allowed_symbols", mode="before")
    @classmethod
    def _split_symbols(cls, value: str | set[str]) -> set[str]:
        if isinstance(value, set):
            return value
        return {symbol.strip() for symbol in value.split(",") if symbol.strip()}

    @field_validator("entry_policy")
    @classmethod
    def _validate_policy(cls, value: str) -> str:
        if value not in {"ignore"}:
            raise ValueError("unsupported ENTRY_POLICY; expected 'ignore'")
        return value


class WebhookBase(BaseModel):
    token: str
    event_id: str
    ts: datetime
    mode: Literal["ENTRY", "CLOSE"]
    symbol: str

    @field_validator("ts")
    @classmethod
    def ensure_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


class EntryPayload(WebhookBase):
    mode: Literal["ENTRY"]
    side: Literal["BUY", "SELL"]
    size: float

    @field_validator("size")
    @classmethod
    def validate_size(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("size must be positive")
        return value


class ClosePayload(WebhookBase):
    mode: Literal["CLOSE"]


WebhookPayloadType = Annotated[Union[EntryPayload, ClosePayload], Field(discriminator="mode")]
PayloadAdapter = TypeAdapter(WebhookPayloadType)


class WebhookResponse(BaseModel):
    status: str
    event_id: str
    mode: str
    detail: Optional[str] = None
    latency_ms: Optional[float] = None


def create_app() -> FastAPI:
    setup_logging(LOG_DIR)
    app = FastAPI(default_response_class=ORJSONResponse)

    settings = Settings.model_validate(os.environ)

    logger.info(
        "gmo_api_key_length_check",
        length=len(settings.gmo_api_key),
        repr_len=len(repr(settings.gmo_api_key)),
        expected=32,
    )
    logger.info(
        "gmo_api_secret_length_check",
        length=len(settings.gmo_api_secret),
        repr_len=len(repr(settings.gmo_api_secret)),
        expected=64,
    )

    runtime_state = RuntimeState(
        db_path=LOG_DIR / "runtime.db",
        max_skew_seconds=settings.max_skew_seconds,
        qty_step=settings.qty_step,
        entry_policy=settings.entry_policy,
    )

    notifier = DiscordNotifier(settings.discord_webhook_url)
    gmo_client = GMOCoinClient(settings.gmo_api_key, settings.gmo_api_secret)
    ws_supervisor = GMOWebSocketSupervisor(gmo_client, symbol="BTC_JPY")
    process_lock = asyncio.Lock()

    app.state.settings = settings
    app.state.runtime = runtime_state
    app.state.notifier = notifier
    app.state.gmo = gmo_client
    app.state.ws = ws_supervisor
    app.state.process_lock = process_lock

    @app.on_event("startup")
    async def on_startup() -> None:  # pragma: no cover
        logger.info("startup_begin", env=settings.env)
        await ws_supervisor.start()
        logger.info("startup_complete")

    @app.on_event("shutdown")
    async def on_shutdown() -> None:  # pragma: no cover
        logger.info("shutdown_begin")
        await ws_supervisor.stop()
        await notifier.close()
        await gmo_client.close()
        logger.info("shutdown_complete")

    async def get_payload(request: Request) -> WebhookPayloadType:
        try:
            body = await request.json()
        except Exception as exc:  # pragma: no cover - FastAPI handles content-type errors
            raise HTTPException(status_code=400, detail="invalid json") from exc
        try:
            return PayloadAdapter.validate_python(body)
        except ValidationError as exc:  # pragma: no cover - fastapi handles but keep explicit
            raise HTTPException(status_code=400, detail=exc.errors()) from exc

    async def enforce_token(payload: WebhookBase) -> None:
        if payload.token != settings.webhook_token:
            logger.warning("webhook_token_mismatch", event_id=payload.event_id)
            raise HTTPException(status_code=400, detail="token mismatch")

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/status")
    async def status() -> dict[str, Any]:
        state = await runtime_state.get_status()
        state["ws_connected"] = ws_supervisor.connected
        return state

    @app.post("/webhook", response_model=WebhookResponse)
    async def webhook(payload: WebhookPayloadType = Depends(get_payload)) -> WebhookResponse:
        await enforce_token(payload)
        if payload.symbol not in settings.allowed_symbols:
            raise HTTPException(status_code=400, detail="symbol not allowed")

        try:
            runtime_state.ensure_fresh(payload.ts)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        if await runtime_state.is_duplicate(payload.event_id):
            logger.info("duplicate_event", event_id=payload.event_id)
            return WebhookResponse(status="duplicate", event_id=payload.event_id, mode=payload.mode)

        start_time = time.perf_counter()
        async with process_lock:
            if await runtime_state.is_duplicate(payload.event_id):
                return WebhookResponse(status="duplicate", event_id=payload.event_id, mode=payload.mode)

            if payload.mode == "ENTRY":
                response = await handle_entry(payload, runtime_state, gmo_client, notifier)
            else:
                response = await handle_close(payload, runtime_state, gmo_client, notifier)

            await runtime_state.record_event(
                EventRecord(event_id=payload.event_id, ts=payload.ts, mode=payload.mode)
            )

        latency_ms = (time.perf_counter() - start_time) * 1000
        response.latency_ms = latency_ms
        return response

    async def handle_entry(
        payload: EntryPayload,
        runtime_state: RuntimeState,
        gmo_client: GMOCoinClient,
        notifier: DiscordNotifier,
    ) -> WebhookResponse:
        try:
            summary = await gmo_client.get_position_summary(payload.symbol)
            runtime_state.record_retry("position_fetch", True)
        except GMOCoinError as exc:
            runtime_state.record_retry("position_fetch", False)
            await notifier.notify_error(
                title="ENTRY ERROR",
                description=f"event_id={payload.event_id} position fetch failed",
                fields={"code": exc.code, "status": exc.status},
            )
            raise HTTPException(status_code=502, detail="position fetch failed") from exc
        await runtime_state.update_position(summary.net_side, summary.net_qty)
        if not summary.is_flat and runtime_state.entry_policy == "ignore":
            logger.info(
                "entry_ignored_active_position",
                event_id=payload.event_id,
                current_side=summary.net_side,
                current_qty=summary.net_qty,
            )
            return WebhookResponse(
                status="ignored",
                event_id=payload.event_id,
                mode=payload.mode,
                detail="position already open",
            )

        qty = runtime_state.floor_qty(payload.size)
        if qty <= 0:
            raise HTTPException(status_code=400, detail="size below qty_step")

        try:
            result = await gmo_client.place_market_order(payload.symbol, payload.side, qty)
            runtime_state.record_retry("entry_order", True)
        except GMOCoinError as exc:
            runtime_state.record_retry("entry_order", False)
            await notifier.notify_error(
                title="ENTRY ERROR",
                description=f"event_id={payload.event_id}",
                fields={"code": exc.code, "status": exc.status, "retryable": exc.retryable},
            )
            raise HTTPException(status_code=502, detail="entry order failed") from exc

        confirmed = await gmo_client.wait_until_position_matches(payload.symbol, payload.side, qty)
        if confirmed.net_side != payload.side or abs(confirmed.net_qty - qty) > 1e-6:
            logger.warning(
                "entry_confirmation_mismatch",
                event_id=payload.event_id,
                expected_side=payload.side,
                expected_qty=qty,
                confirmed_side=confirmed.net_side,
                confirmed_qty=confirmed.net_qty,
            )
        await runtime_state.update_position(confirmed.net_side, confirmed.net_qty)

        await notifier.notify_success(
            title="ENTRY OK",
            description=f"event_id={payload.event_id} side={payload.side}",
            fields={
                "size": qty,
                "net_side": confirmed.net_side or "FLAT",
                "net_qty": confirmed.net_qty,
            },
        )

        logger.info(
            "entry_completed",
            event_id=payload.event_id,
            side=payload.side,
            size=qty,
            result=result,
        )
        return WebhookResponse(status="ok", event_id=payload.event_id, mode=payload.mode)

    async def handle_close(
        payload: ClosePayload,
        runtime_state: RuntimeState,
        gmo_client: GMOCoinClient,
        notifier: DiscordNotifier,
    ) -> WebhookResponse:
        try:
            summary = await gmo_client.get_position_summary(payload.symbol)
            runtime_state.record_retry("position_fetch", True)
        except GMOCoinError as exc:
            runtime_state.record_retry("position_fetch", False)
            await notifier.notify_error(
                title="CLOSE ERROR",
                description=f"event_id={payload.event_id} position fetch failed",
                fields={"code": exc.code, "status": exc.status},
            )
            raise HTTPException(status_code=502, detail="position fetch failed") from exc
        await runtime_state.update_position(summary.net_side, summary.net_qty)
        if summary.is_flat:
            await notifier.notify_success(
                title="CLOSE OK",
                description=f"event_id={payload.event_id} already flat",
                fields={"closed_qty": 0, "avg_px": "n/a"},
            )
            logger.info("close_already_flat", event_id=payload.event_id)
            return WebhookResponse(status="ok", event_id=payload.event_id, mode=payload.mode, detail="already flat")

        opposite_side = "SELL" if summary.net_side == "BUY" else "BUY"
        closed_qty = 0.0
        for position in summary.positions:
            position_size = float(position.get("size") or position.get("positionSize") or 0.0)
            if position_size <= 0:
                continue
            try:
                await gmo_client.place_market_order(
                    payload.symbol,
                    opposite_side,
                    position_size,
                    settle_positions=[position],
                )
                runtime_state.record_retry("close_order", True)
                closed_qty += position_size
            except GMOCoinError as exc:
                runtime_state.record_retry("close_order", False)
                if exc.retryable:
                    raise HTTPException(status_code=502, detail="close order retryable failure") from exc
                await notifier.notify_error(
                    title="CLOSE ERROR",
                    description=f"event_id={payload.event_id}",
                    fields={"code": exc.code, "status": exc.status},
                )
                raise HTTPException(status_code=502, detail="close order failed") from exc

        confirmed = await gmo_client.wait_until_position_matches(payload.symbol, None, 0.0)
        if not confirmed.is_flat:
            logger.warning(
                "close_confirmation_mismatch",
                event_id=payload.event_id,
                final_side=confirmed.net_side,
                final_qty=confirmed.net_qty,
            )
        await runtime_state.update_position(confirmed.net_side, confirmed.net_qty)

        await notifier.notify_success(
            title="CLOSE OK",
            description=f"event_id={payload.event_id}",
            fields={
                "closed_qty": round(closed_qty, 8),
                "avg_px": "market",
                "net_qty": confirmed.net_qty,
            },
        )

        logger.info(
            "close_completed",
            event_id=payload.event_id,
            closed_qty=closed_qty,
            final_side=confirmed.net_side,
            final_qty=confirmed.net_qty,
        )
        return WebhookResponse(status="ok", event_id=payload.event_id, mode=payload.mode)

    return app


app = create_app()

