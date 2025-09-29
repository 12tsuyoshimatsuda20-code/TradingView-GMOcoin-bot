from __future__ import annotations

import os
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from loguru import logger

from gmo_client import GMOClient, GMOClientError
from notify import Notifier
from schemas import (
    HealthResponse,
    Mode,
    StatusResponse,
    WebhookPayload,
    WebhookResponse,
)
from storage import Storage
from utils import calculate_latency_ms, is_timestamp_fresh, quantize_down, utcnow


class Settings:
    def __init__(self) -> None:
        self.webhook_token = os.getenv("WEBHOOK_TOKEN", "")
        self.gmo_api_key = os.getenv("GMO_API_KEY", "")
        self.gmo_api_secret = os.getenv("GMO_API_SECRET", "")
        self.max_skew_seconds = int(os.getenv("MAX_SKEW_SECONDS", "60"))
        self.qty_step = float(os.getenv("QTY_STEP", "0.01"))
        self.event_ttl_seconds = int(os.getenv("EVENT_TTL_SECONDS", "600"))
        self.env = os.getenv("ENV", "development")
        self.allowed_source_ips: List[str] = [
            ip.strip() for ip in os.getenv("ALLOWED_SOURCE_IPS", "").split(",") if ip.strip()
        ]
        self.log_level = os.getenv("LOG_LEVEL", "INFO")
        self.runtime_dir = Path(os.getenv("RUNTIME_DIR", "/runtime"))
        self.sqlite_path = self.runtime_dir / "exec_lane.sqlite3"
        if not self.webhook_token:
            raise RuntimeError("WEBHOOK_TOKEN must be configured")


settings = Settings()

logger.remove()
logger.add(
    sys.stdout,
    serialize=True,
    level=settings.log_level.upper(),
    backtrace=False,
    diagnose=False,
)

storage = Storage(settings.sqlite_path)
notifier = Notifier(os.getenv("DISCORD_WEBHOOK_URL"))
gmo_client = GMOClient(settings.gmo_api_key, settings.gmo_api_secret, storage)


@dataclass
class HandlerResult:
    response: WebhookResponse
    event_status: str
    notify_level: Optional[str] = None
    notify_title: Optional[str] = None
    notify_message: Optional[str] = None
    notify_extra: Optional[dict] = None


@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ANN001
    await storage.connect()
    await storage.init()
    await notifier.start()
    try:
        await gmo_client.start()
    except Exception as exc:  # noqa: BLE001
        logger.error("failed to start gmo client", error=str(exc))
        await notifier.send(
            "error",
            "GMO client initialization failed",
            str(exc),
        )
    try:
        yield
    finally:
        await notifier.close()
        await gmo_client.close()
        await storage.close()


app = FastAPI(lifespan=lifespan)


async def ensure_client_ip(request: Request) -> None:
    if not settings.allowed_source_ips:
        return
    client_host = request.client.host if request.client else None
    if client_host not in settings.allowed_source_ips:
        logger.warning("unauthorized source ip", client=client_host)
        raise HTTPException(status_code=403, detail="unauthorized source")


@app.get("/healthz", response_model=HealthResponse)
async def healthz() -> HealthResponse:
    return HealthResponse()


@app.get("/status", response_model=StatusResponse)
async def status_endpoint() -> StatusResponse:
    snapshot = await storage.get_status()
    return StatusResponse(
        position_qty=snapshot.position_qty,
        position_side=snapshot.position_side,
        last_event_id=snapshot.last_event_id,
        last_event_ts=snapshot.last_event_ts,
        retry_stats=snapshot.retry_stats,
        ws_connected=snapshot.ws_connected,
        env=settings.env,
    )


@app.post("/webhook", response_model=WebhookResponse)
async def webhook_handler(
    payload: WebhookPayload,
    request: Request,
    _: None = Depends(ensure_client_ip),
) -> WebhookResponse:
    start_time = utcnow()
    event_started = False
    if payload.token != settings.webhook_token:
        logger.warning("invalid token", event_id=payload.event_id)
        raise HTTPException(status_code=401, detail="invalid token")

    if not is_timestamp_fresh(payload.ts, settings.max_skew_seconds):
        logger.warning("stale timestamp", event_id=payload.event_id, ts=str(payload.ts))
        raise HTTPException(status_code=422, detail="stale timestamp")

    is_new = await storage.begin_event(payload.event_id, payload.ts, payload.mode.value, settings.event_ttl_seconds)
    event_started = True
    if not is_new:
        logger.info("duplicate event", event_id=payload.event_id)
        await storage.finalize_event(payload.event_id, status="duplicate")
        return WebhookResponse(ok=True, duplicate=True, message="duplicate", event_id=payload.event_id)

    try:
        if payload.mode == Mode.ENTRY:
            result = await handle_entry(payload)
        elif payload.mode == Mode.CLOSE:
            result = await handle_close(payload)
        else:
            raise HTTPException(status_code=400, detail="unsupported mode")
        latency = calculate_latency_ms(start_time)
        logger.bind(
            event_id=payload.event_id,
            action=payload.mode.value,
            symbol=payload.symbol,
            result="success",
            latency_ms=latency,
        ).info("webhook processed")
        await storage.finalize_event(payload.event_id, status=result.event_status)
        await storage.update_status(
            last_event_id=payload.event_id,
            last_event_ts=payload.ts.isoformat(),
        )
        if result.notify_level and result.notify_title and result.notify_message:
            await notifier.send(
                result.notify_level,
                result.notify_title,
                result.notify_message,
                extra=result.notify_extra,
            )
        return result.response
    except GMOClientError as exc:
        latency = calculate_latency_ms(start_time)
        payload_data = exc.payload
        logger.bind(
            event_id=payload.event_id,
            action=payload.mode.value,
            symbol=payload.symbol,
            result="error",
            latency_ms=latency,
            code=payload_data.get("message_code"),
            message=payload_data.get("message_string"),
        ).error("gmo client error")
        await notifier.send(
            "error",
            f"{payload.mode.value} failed",
            payload_data.get("message_string", "GMO API error"),
            extra={"message_code": payload_data.get("message_code")},
        )
        await storage.finalize_event(
            payload.event_id,
            status="error",
            error_code=payload_data.get("message_code"),
            error_detail=payload_data.get("message_string"),
        )
        raise HTTPException(status_code=502, detail="gmo api error") from exc
    except HTTPException:
        if event_started:
            await storage.finalize_event(payload.event_id, status="rejected")
        raise
    except Exception as exc:  # noqa: BLE001
        latency = calculate_latency_ms(start_time)
        logger.bind(
            event_id=payload.event_id,
            action=payload.mode.value,
            symbol=payload.symbol,
            result="exception",
            latency_ms=latency,
        ).exception("webhook handling exception")
        await notifier.send(
            "error",
            f"{payload.mode.value} exception",
            str(exc),
        )
        if event_started:
            await storage.finalize_event(
                payload.event_id,
                status="error",
                error_detail=str(exc),
            )
        raise HTTPException(status_code=500, detail="internal error") from exc


async def handle_entry(payload: WebhookPayload) -> HandlerResult:
    status_snapshot = await storage.get_status()
    if status_snapshot.position_qty and status_snapshot.position_qty > 0:
        logger.info("entry ignored due to existing position", qty=status_snapshot.position_qty)
        return HandlerResult(
            response=WebhookResponse(
                ok=True,
                ignored=True,
                message="position exists",
                event_id=payload.event_id,
            ),
            event_status="ignored",
        )

    assert payload.size is not None
    assert payload.side is not None
    quantized = quantize_down(payload.size, settings.qty_step)
    if quantized <= 0:
        logger.warning("entry size zero after quantization", raw_size=payload.size)
        return HandlerResult(
            response=WebhookResponse(
                ok=True,
                ignored=True,
                message="size too small",
                event_id=payload.event_id,
            ),
            event_status="skipped",
        )

    try:
        gmo_response = await gmo_client.market_entry(payload.symbol, payload.side.value, quantized)
    except GMOClientError:
        raise
    return HandlerResult(
        response=WebhookResponse(
            ok=True,
            event_id=payload.event_id,
            gmo_response=gmo_response,
        ),
        event_status="success",
        notify_level="info",
        notify_title=f"ENTRY {payload.side.value}",
        notify_message=f"Submitted {quantized} {payload.symbol}",
        notify_extra={"event_id": payload.event_id},
    )


async def handle_close(payload: WebhookPayload) -> HandlerResult:
    status_snapshot = await storage.get_status()
    if not status_snapshot.position_qty or status_snapshot.position_qty <= 0:
        logger.info("close ignored - no open position")
        return HandlerResult(
            response=WebhookResponse(
                ok=True,
                ignored=True,
                message="no position",
                event_id=payload.event_id,
            ),
            event_status="ignored",
        )
    try:
        gmo_response = await gmo_client.market_close_all(payload.symbol)
    except GMOClientError:
        raise
    return HandlerResult(
        response=WebhookResponse(
            ok=True,
            event_id=payload.event_id,
            gmo_response=gmo_response,
        ),
        event_status="success",
        notify_level="info",
        notify_title="CLOSE ALL",
        notify_message=f"Closed {status_snapshot.position_qty} {payload.symbol}",
        notify_extra={"event_id": payload.event_id},
    )
