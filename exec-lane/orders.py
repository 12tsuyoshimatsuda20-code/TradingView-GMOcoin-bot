from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Dict, Iterable, Optional

from loguru import logger

from gmo_client import GMOCoinClient, GMOCoinAPIError
from settings import settings


class QuantityStepError(ValueError):
    """Raised when the requested size does not match the configured step."""


class EntryIgnored(Exception):
    """Raised when an ENTRY request should be ignored due to existing position."""


class OrderExecutionError(RuntimeError):
    """Raised when an order placement fails."""


def ensure_qty_step(size: Decimal, step: Decimal) -> None:
    if size <= 0:
        raise QuantityStepError("Size must be greater than zero")
    remainder = (size % step).normalize()
    if remainder != 0:
        raise QuantityStepError(f"Size {size} is not a multiple of step {step}")


def is_flat(positions: Dict[str, Any]) -> bool:
    data: Iterable[Dict[str, Any]] = positions.get("data", []) if isinstance(positions, dict) else []
    net = Decimal("0")
    for item in data:
        try:
            side = item.get("side")
            size = Decimal(str(item.get("size")))
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Unexpected position payload", error=str(exc), item=item)
            continue
        if side == "BUY":
            net += size
        elif side == "SELL":
            net -= size
    return net == 0


@dataclass
class OrderService:
    client: GMOCoinClient

    async def entry_if_flat(self, side: Optional[str], size: Decimal) -> Dict[str, Any]:
        if side not in {"BUY", "SELL"}:
            raise QuantityStepError("side must be BUY or SELL for entry")
        ensure_qty_step(size, settings.qty_step)

        positions = await self.client.get_open_positions(settings.symbol)
        if not is_flat(positions):
            logger.info("ENTRY ignored due to existing position", side=side, size=str(size))
            raise EntryIgnored("Position is not flat")

        size_str = self._decimal_to_str(size)
        try:
            result = await self.client.place_market_entry(settings.symbol, side, size_str)
        except GMOCoinAPIError as exc:
            raise OrderExecutionError(str(exc)) from exc
        return result

    async def close_all(self) -> Dict[str, Any]:
        positions = await self.client.get_open_positions(settings.symbol)
        if is_flat(positions):
            logger.info("CLOSE skipped because position is already flat")
            return {"status": "already_flat", "data": positions.get("data", [])}
        try:
            result = await self.client.place_market_close_all(settings.symbol)
        except GMOCoinAPIError as exc:
            raise OrderExecutionError(str(exc)) from exc
        return result

    @staticmethod
    def _decimal_to_str(value: Decimal) -> str:
        return format(value.normalize(), "f")
