"""FastAPI entrypoint exposing webhook endpoints."""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

import pybotters
from fastapi import Depends, FastAPI, HTTPException, Request
from zoneinfo import ZoneInfo

from .gmo_client import GMOClient
from .logger import configure_logging, get_logger
from .models import HealthResponse, StatusResponse, TradingViewSignal, WebhookResponse
from .service import TradingService
from .settings import Settings, get_settings
from .store import EventStore

logger = get_logger(__name__)


def verify_token(request: Request, settings: Settings = Depends(get_settings)) -> None:
    token = request.query_params.get("token") or request.headers.get("X-TV-Token")
    if not token or token != settings.webhook_token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="unauthorized")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings.log_level, settings.timezone)
    logger.info("Starting application", extra={"allowedSymbols": settings.allowed_symbols})

    client = pybotters.Client(apis={"gmocoin": (settings.gmo_api_key, settings.gmo_api_secret)})
    store = EventStore(Path("data") / "bot.db")
    await store.init()
    gmo_client = GMOClient(client, api_base=settings.api_base)
    service = TradingService(settings=settings, gmo_client=gmo_client, store=store)

    app.state.settings = settings
    app.state.client = client
    app.state.store = store
    app.state.service = service

    try:
        yield
    finally:
        await client.close()
        logger.info("Application shutdown")


app = FastAPI(title="TradingView GMO Coin Bot", lifespan=lifespan)


@app.get("/healthz", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse()


@app.get("/status", response_model=StatusResponse)
async def status_endpoint(request: Request) -> StatusResponse:
    settings: Settings = request.app.state.settings
    store: EventStore = request.app.state.store
    last_event = await store.get_last_event()
    return StatusResponse(
        trading_enabled=settings.trading_enabled,
        allowed_symbols=settings.allowed_symbols,
        last_event=last_event,
        server_time=datetime.now(ZoneInfo(settings.timezone)),
    )


@app.post("/webhook/tv", response_model=WebhookResponse)
async def webhook(
    signal: TradingViewSignal,
    request: Request,
    _: Any = Depends(verify_token),
) -> WebhookResponse:
    service: TradingService = request.app.state.service
    return await service.process(signal)
