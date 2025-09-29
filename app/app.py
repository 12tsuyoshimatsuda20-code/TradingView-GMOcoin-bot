from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

import pybotters
from fastapi import Depends, FastAPI, Request

from .config import settings
from .discord import DiscordNotifier
from .gmo import GMOCoinClient
from .idempotency import IdempotencyStore
from .logging import get_logger, setup_logging
from .schemas import WebhookPayload, WebhookResponse
from .service import TradingBotService

setup_logging()
logger = get_logger()

try:
    import uvloop  # type: ignore

    uvloop.install()
    logger.info("uvloop installed", extra={"uvloop": True})
except Exception:
    logger.warning("uvloop unavailable", extra={"uvloop": False})


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.ensure_valid()
    apis = {"gmocoin": (settings.gmo_api_key, settings.gmo_api_secret)}
    client = pybotters.Client(apis=apis, base_url="https://api.coin.z.com")
    notifier = DiscordNotifier()
    idempotency = IdempotencyStore(settings.idempotency_ttl)
    service = TradingBotService(
        gmo_client=GMOCoinClient(client, symbol="BTC_JPY"),
        notifier=notifier,
        idempotency_store=idempotency,
    )
    app.state.service = service
    app.state.pyb_client = client
    try:
        yield
    finally:
        await service.shutdown()
        await client.close()


def get_service(request: Request) -> TradingBotService:
    return request.app.state.service  # type: ignore[return-value]


app = FastAPI(lifespan=lifespan)


@app.post("/webhook", response_model=WebhookResponse)
async def webhook_endpoint(payload: WebhookPayload, service: TradingBotService = Depends(get_service)) -> WebhookResponse:
    response = await service.handle_webhook(payload)
    return response


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/status")
async def status(service: TradingBotService = Depends(get_service)) -> dict[str, Any]:
    return await service.status()
