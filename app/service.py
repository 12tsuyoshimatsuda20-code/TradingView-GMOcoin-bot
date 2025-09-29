from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Dict, Optional

from fastapi import HTTPException

from .config import settings
from .discord import build_error_message, build_success_message, DiscordNotifier
from .gmo import GMOCoinClient, GMOAPIError
from .idempotency import IdempotencyStore
from .logging import get_logger
from .schemas import WebhookPayload, WebhookResponse
from .utils import calculate_latency_ms, floor_to_step, utcnow

logger = get_logger()


@dataclass
class ServiceStatus:
    last_event: Optional[dict] = None
    ws_connected: bool = False
    retry_stats: Dict[str, Any] = field(default_factory=dict)


class TradingBotService:
    def __init__(
        self,
        *,
        gmo_client: GMOCoinClient,
        notifier: DiscordNotifier,
        idempotency_store: IdempotencyStore,
    ) -> None:
        self._gmo = gmo_client
        self._notifier = notifier
        self._idempotency = idempotency_store
        self._lock = asyncio.Lock()
        self._status = ServiceStatus(
            retry_stats={"rest_retry_limit": settings.retry_limit}
        )

    async def handle_webhook(self, payload: WebhookPayload) -> WebhookResponse:
        now = utcnow()
        skew = abs((now - payload.timestamp()).total_seconds())
        if skew > settings.max_skew_seconds:
            raise HTTPException(status_code=400, detail="timestamp skew exceeded")

        is_new, record = await self._idempotency.check_and_set(
            payload.event_id,
            status="received",
        )
        if not is_new:
            return WebhookResponse(
                status="duplicate",
                detail="event already processed",
                event_id=payload.event_id,
                duplicate=True,
            )

        async with self._lock:
            if payload.mode == "ENTRY":
                response = await self._process_entry(payload)
            else:
                response = await self._process_close(payload)
        return response

    async def _process_entry(self, payload: WebhookPayload) -> WebhookResponse:
        start = utcnow()
        try:
            summary = await self._gmo.get_open_positions()
        except GMOAPIError as exc:
            await self._idempotency.update(payload.event_id, "failed", detail=exc.detail)
            await self._notify_error(
                title="ERROR",
                fields={
                    "event_id": payload.event_id,
                    "mode": payload.mode,
                    "symbol": payload.symbol,
                    "code": exc.code,
                    "detail": exc.detail or "",
                },
            )
            raise HTTPException(status_code=502, detail="failed to fetch positions") from exc

        if not summary.is_flat:
            await self._idempotency.update(payload.event_id, "ignored", detail="position-open")
            self._status.last_event = {
                "event_id": payload.event_id,
                "status": "ignored",
                "detail": "position already open",
                "mode": payload.mode,
                "timestamp": utcnow().isoformat(),
            }
            logger.info(
                "entry ignored due to existing position",
                extra={
                    "event_id": payload.event_id,
                    "mode": payload.mode,
                    "position_side": summary.side,
                    "position_size": str(summary.size),
                },
            )
            return WebhookResponse(
                status="ignored",
                detail="position already open",
                event_id=payload.event_id,
                ignored=True,
            )

        if payload.size is None:
            raise HTTPException(status_code=400, detail="size required")
        normalized_size = floor_to_step(payload.size, settings.qty_step)
        if normalized_size <= 0:
            await self._idempotency.update(payload.event_id, "failed", detail="size-too-small")
            raise HTTPException(status_code=400, detail="size below minimum step")

        try:
            result = await self._submit_entry(payload.side, normalized_size)
        except GMOAPIError as exc:
            await self._idempotency.update(payload.event_id, "failed", detail=exc.detail)
            await self._notify_error(
                title="ERROR",
                fields={
                    "event_id": payload.event_id,
                    "mode": payload.mode,
                    "symbol": payload.symbol,
                    "code": exc.code,
                    "detail": exc.detail or "",
                },
            )
            raise HTTPException(status_code=502, detail="entry order failed") from exc

        latency = calculate_latency_ms(start)
        await self._idempotency.update(payload.event_id, "executed", detail="entry-success")
        self._status.last_event = {
            "event_id": payload.event_id,
            "status": "executed",
            "detail": "entry",
            "mode": payload.mode,
            "symbol": payload.symbol,
            "latency_ms": latency,
        }

        await self._notify_success(
            title="ENTRY OK",
            fields={
                "event_id": payload.event_id,
                "symbol": payload.symbol,
                "side": payload.side,
                "size": normalized_size,
                "latency_ms": latency,
            },
        )
        return WebhookResponse(
            status="executed",
            detail="entry order placed",
            event_id=payload.event_id,
            payload={"order": result.get("data")},
        )

    async def _process_close(self, payload: WebhookPayload) -> WebhookResponse:
        start = utcnow()
        try:
            summary = await self._gmo.get_open_positions()
        except GMOAPIError as exc:
            await self._idempotency.update(payload.event_id, "failed", detail=exc.detail)
            await self._notify_error(
                title="ERROR",
                fields={
                    "event_id": payload.event_id,
                    "mode": payload.mode,
                    "symbol": payload.symbol,
                    "code": exc.code,
                    "detail": exc.detail or "",
                },
            )
            raise HTTPException(status_code=502, detail="failed to fetch positions") from exc

        if summary.is_flat:
            await self._idempotency.update(payload.event_id, "noop", detail="already-flat")
            latency = calculate_latency_ms(start)
            self._status.last_event = {
                "event_id": payload.event_id,
                "status": "noop",
                "detail": "already flat",
                "mode": payload.mode,
                "latency_ms": latency,
            }
            await self._notify_success(
                title="CLOSE OK",
                fields={
                    "event_id": payload.event_id,
                    "symbol": payload.symbol,
                    "closed_qty": Decimal("0"),
                    "latency_ms": latency,
                },
            )
            return WebhookResponse(
                status="noop",
                detail="already flat",
                event_id=payload.event_id,
            )

        try:
            result = await self._gmo.submit_close(summary.positions)
        except GMOAPIError as exc:
            await self._idempotency.update(payload.event_id, "failed", detail=exc.detail)
            await self._notify_error(
                title="ERROR",
                fields={
                    "event_id": payload.event_id,
                    "mode": payload.mode,
                    "symbol": payload.symbol,
                    "code": exc.code,
                    "detail": exc.detail or "",
                },
            )
            raise HTTPException(status_code=502, detail="close order failed") from exc

        latency = calculate_latency_ms(start)
        await self._idempotency.update(payload.event_id, "executed", detail="close-success")
        self._status.last_event = {
            "event_id": payload.event_id,
            "status": "executed",
            "detail": "close",
            "mode": payload.mode,
            "symbol": payload.symbol,
            "latency_ms": latency,
        }
        await self._notify_success(
            title="CLOSE OK",
            fields={
                "event_id": payload.event_id,
                "symbol": payload.symbol,
                "closed_qty": summary.size,
                "latency_ms": latency,
            },
        )
        return WebhookResponse(
            status="executed",
            detail="close order placed",
            event_id=payload.event_id,
            payload={"order": result.get("data")},
        )

    async def _submit_entry(self, side: str | None, size: Decimal) -> dict:
        if settings.dry_run:
            logger.info(
                "dry-run entry order",
                extra={"side": side, "size": str(size)},
            )
            return {"status": 0, "data": {"dry_run": True}}
        if not side:
            raise ValueError("side required")
        return await self._gmo.submit_entry(side=side, size=size)

    async def shutdown(self) -> None:
        await self._notifier.close()

    async def status(self) -> dict:
        last_event = self._status.last_event
        return {
            "last_event": last_event,
            "ws_connected": self._status.ws_connected,
            "retry_stats": self._status.retry_stats,
        }

    async def _notify_success(self, *, title: str, fields: Dict[str, Any]) -> None:
        message = build_success_message(title=title, fields=fields)
        await self._notifier.send(message)

    async def _notify_error(self, *, title: str, fields: Dict[str, Any]) -> None:
        message = build_error_message(title=title, fields=fields)
        await self._notifier.send(message)
