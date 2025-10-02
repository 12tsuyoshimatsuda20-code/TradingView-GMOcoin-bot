"""Trading logic for processing TradingView webhook signals."""
from __future__ import annotations

import asyncio
import time
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from typing import Tuple

from fastapi import HTTPException, status

from .gmo_client import GMOClient, GMOAPIError
from .logger import get_logger
from .models import (
    CloseSignal,
    EntrySignal,
    TradingViewSignal,
    WebhookResponse,
)
from .settings import Settings
from .store import EventStore


class TradingService:
    """Coordinate validation, idempotency, and order placement."""

    def __init__(
        self,
        *,
        settings: Settings,
        gmo_client: GMOClient,
        store: EventStore,
    ) -> None:
        self._settings = settings
        self._gmo = gmo_client
        self._store = store
        self._lock = asyncio.Lock()
        self._log = get_logger(__name__)

    async def process(self, signal: TradingViewSignal) -> WebhookResponse:
        self._validate_symbol(signal.symbol)
        self._validate_timestamp(signal.ts)

        received_at = int(time.time() * 1000)
        async with self._lock:
            if isinstance(signal, EntrySignal):
                normalized_size = self._normalize_size(signal.symbol, signal.size)
            else:
                normalized_size = None

            inserted = await self._store.record_event(
                event_id=signal.id,
                event_type=signal.type,
                symbol=signal.symbol,
                side=signal.side,
                size=normalized_size,
                ts=signal.ts,
                received_at=received_at,
            )
            duplicated = not inserted
            dry_run = not self._settings.trading_enabled
            if duplicated:
                self._log.info(
                    "Duplicate signal ignored",
                    extra={"id": signal.id, "type": signal.type, "symbol": signal.symbol},
                )
                return WebhookResponse(
                    id=signal.id,
                    type=signal.type,
                    symbol=signal.symbol,
                    side=signal.side,
                    dry_run=dry_run,
                    duplicated=True,
                    executed=False,
                    message="duplicate ignored",
                )

            if isinstance(signal, EntrySignal):
                executed, message = await self._handle_entry(signal, normalized_size, dry_run)
            else:
                executed, message = await self._handle_close(signal, dry_run)

            return WebhookResponse(
                id=signal.id,
                type=signal.type,
                symbol=signal.symbol,
                side=signal.side,
                dry_run=dry_run,
                duplicated=False,
                executed=executed,
                message=message,
            )

    def _validate_symbol(self, symbol: str) -> None:
        if symbol.upper() not in self._settings.allowed_symbols:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="symbol not allowed")

    def _validate_timestamp(self, ts: int) -> None:
        now_ms = int(time.time() * 1000)
        if abs(now_ms - ts) > self._settings.max_skew_seconds * 1000:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail="timestamp skew too large")

    def _normalize_size(self, symbol: str, size: str) -> str:
        decimals = self._settings.get_size_decimals(symbol)
        quant = Decimal(1).scaleb(-decimals)
        try:
            size_decimal = Decimal(size)
        except InvalidOperation as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail="invalid size") from exc
        if size_decimal <= 0:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail="size must be positive")
        normalized = size_decimal.quantize(quant, rounding=ROUND_DOWN)
        if normalized != size_decimal:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"size must align with {decimals} decimal places",
            )
        return f"{normalized:.{decimals}f}"

    async def _handle_entry(self, signal: EntrySignal, size: str, dry_run: bool) -> Tuple[bool, str]:
        self._log.info(
            "Processing ENTRY",
            extra={"id": signal.id, "symbol": signal.symbol, "side": signal.side, "size": size},
        )
        if dry_run:
            return False, "dry run"
        try:
            result = await self._gmo.submit_market_entry(
                symbol=signal.symbol,
                side=signal.side,
                size=size,
            )
            self._log.info(
                "ENTRY order accepted",
                extra={"id": signal.id, "symbol": signal.symbol, "result": result.get("status")},
            )
            return True, "entry submitted"
        except GMOAPIError as exc:
            self._log.error(
                "ENTRY order failed",
                extra={"id": signal.id, "symbol": signal.symbol, "error": str(exc)},
            )
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail="entry order failed")

    async def _handle_close(self, signal: CloseSignal, dry_run: bool) -> Tuple[bool, str]:
        self._log.info(
            "Processing CLOSE",
            extra={"id": signal.id, "symbol": signal.symbol, "side": signal.side},
        )
        target_side = "BUY" if signal.side == "SELL" else "SELL"
        try:
            positions = await self._gmo.fetch_open_positions(symbol=signal.symbol)
        except GMOAPIError as exc:
            self._log.error(
                "Failed to fetch positions",
                extra={"symbol": signal.symbol, "error": str(exc)},
            )
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail="position query failed")

        decimals = self._settings.get_size_decimals(signal.symbol)
        quant = Decimal(1).scaleb(-decimals)
        total = Decimal("0")
        for pos in positions:
            if pos.get("side") == target_side:
                try:
                    total += Decimal(str(pos.get("size")))
                except Exception as exc:  # pragma: no cover - unexpected payload
                    self._log.warning(
                        "Unexpected position size",
                        extra={"position": pos, "error": str(exc)},
                    )
        if total <= 0:
            self._log.info(
                "No positions to close",
                extra={"symbol": signal.symbol, "targetSide": target_side},
            )
            return False, "no positions"

        normalized = total.quantize(quant, rounding=ROUND_DOWN)
        if normalized <= 0:
            self._log.info(
                "No positions to close after normalization",
                extra={"symbol": signal.symbol, "targetSide": target_side},
            )
            return False, "no positions"

        size_str = f"{normalized:.{decimals}f}"
        await self._store.update_size(signal.id, size_str)
        if dry_run:
            self._log.info(
                "Dry run close",
                extra={"symbol": signal.symbol, "side": signal.side, "size": size_str},
            )
            return False, "dry run"

        try:
            result = await self._gmo.submit_close_bulk_order(
                symbol=signal.symbol,
                side=signal.side,
                size=size_str,
            )
            self._log.info(
                "CLOSE order accepted",
                extra={"id": signal.id, "symbol": signal.symbol, "result": result.get("status")},
            )
            return True, "close submitted"
        except GMOAPIError as exc:
            self._log.error(
                "CLOSE order failed",
                extra={"id": signal.id, "symbol": signal.symbol, "error": str(exc)},
            )
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail="close order failed")
