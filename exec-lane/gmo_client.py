from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from typing import Any, Dict, List, Optional, Tuple

import pybotters
from loguru import logger

from .store import StatusStore


@dataclass
class OrderResult:
    order_id: str
    status: str
    data: Dict[str, Any]


@dataclass
class PositionSummary:
    side: str
    size: float
    average_price: Optional[float] = None


class GMOCoinClient:
    BASE_URL = "https://api.coin.z.com"

    def __init__(
        self,
        client: pybotters.Client,
        status_store: StatusStore,
        *,
        rest_timeout: float = 5.0,
    ) -> None:
        self._client = client
        self._status_store = status_store
        self._rest_timeout = rest_timeout

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Dict[str, Any] | None = None,
        json_body: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        url = f"{self.BASE_URL}{path}"
        backoff = [0.5, 1.0, 2.0]
        for attempt, delay in enumerate(backoff, start=1):
            try:
                async with self._client.request(
                    method,
                    url,
                    params=params,
                    json=json_body,
                    timeout=self._rest_timeout,
                ) as resp:
                    text = await resp.text()
                    if resp.status >= 500 or resp.status == 429:
                        await self._status_store.incr_retry("rest")
                        logger.warning(
                            "REST retry",
                            extra={
                                "path": path,
                                "status": resp.status,
                                "body": text,
                                "attempt": attempt,
                            },
                        )
                        if attempt == len(backoff):
                            resp.raise_for_status()
                        await asyncio.sleep(delay)
                        continue
                    if resp.status >= 400:
                        logger.error(
                            "REST error",
                            extra={"path": path, "status": resp.status, "body": text},
                        )
                        resp.raise_for_status()
                    data = await resp.json()
                    return data
            except Exception as exc:
                if attempt == len(backoff):
                    logger.exception("REST request failed", exc_info=exc)
                    raise
                await self._status_store.incr_retry("rest")
                logger.warning(
                    "REST request exception, retrying",
                    extra={"path": path, "attempt": attempt, "error": repr(exc)},
                )
                await asyncio.sleep(delay)
        raise RuntimeError("Unreachable")

    async def fetch_position_summary(self, symbol: str) -> PositionSummary:
        data = await self._request("GET", "/private/v1/positionSummary", params={"symbol": symbol})
        summary = data.get("data") or {}
        positions = summary.get("list") or summary.get("positionSummary") or []
        if isinstance(positions, dict):
            positions = [positions]
        for entry in positions:
            if entry.get("symbol") != symbol:
                continue
            side = entry.get("side", "NONE")
            size = float(entry.get("size") or entry.get("quantity") or 0)
            price_raw = entry.get("averagePrice") or entry.get("average_price")
            avg_price = float(price_raw) if price_raw is not None else None
            if size > 0:
                await self._status_store.set_position(size, side)
                return PositionSummary(side=side, size=size, average_price=avg_price)
        await self._status_store.set_position(0.0, "FLAT")
        return PositionSummary(side="FLAT", size=0.0)

    async def fetch_settlement_size(
        self, symbol: str
    ) -> Tuple[float, float, List[Dict[str, str]]]:
        data = await self._request(
            "GET", "/private/v1/openPositions", params={"symbol": symbol}
        )
        entries = data.get("data") or {}
        positions = entries.get("list") or entries.get("openPositions") or []
        total_size = Decimal("0")
        settable = Decimal("0")
        settle_payload: List[Dict[str, str]] = []
        for entry in positions:
            raw_size = entry.get("size") or entry.get("quantity") or 0.0
            size_dec = Decimal(str(raw_size))
            if size_dec <= 0:
                continue
            position_id = entry.get("positionId") or entry.get("position_id") or entry.get("positionNo")
            if not position_id:
                continue
            settle_entry_raw = entry.get("settleQuantity") or entry.get("settableQuantity")
            settle_dec = (
                Decimal(str(settle_entry_raw)) if settle_entry_raw is not None else size_dec
            )
            if settle_dec > size_dec:
                settle_dec = size_dec
            quantized_total = size_dec.quantize(Decimal("0.01"), rounding=ROUND_DOWN)
            if quantized_total <= 0:
                continue
            total_size += quantized_total
            quantized_settle = settle_dec.quantize(Decimal("0.01"), rounding=ROUND_DOWN)
            if quantized_settle <= 0:
                continue
            settable += quantized_settle
            settle_payload.append(
                {
                    "positionId": str(position_id),
                    "size": format(quantized_settle, ".2f"),
                }
            )
        return float(total_size), float(settable), settle_payload

    async def place_market_order(
        self,
        *,
        symbol: str,
        side: str | None = None,
        size: float | Decimal | None = None,
        settle_position: List[Dict[str, str]] | None = None,
    ) -> OrderResult:
        payload: Dict[str, Any] = {
            "symbol": symbol,
            "executionType": "MARKET",
        }
        if side is not None:
            payload["side"] = side
        if size is not None:
            size_dec = Decimal(str(size)).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
            payload["size"] = format(size_dec, ".2f")
        if settle_position:
            payload["settlePosition"] = settle_position
        data = await self._request("POST", "/private/v1/order", json_body=payload)
        status_value = data.get("status")
        if status_value not in (0, "0", "SUCCESS", "success"):
            message = data.get("messages") or data.get("message") or data
            raise RuntimeError(f"Order rejected: {message}")
        order_data = data.get("data") or {}
        order_id = order_data.get("orderId") or order_data.get("order_id") or ""
        return OrderResult(order_id=order_id, status=str(status_value), data=order_data)

    async def cancel_all(self, symbol: str) -> None:
        await self._request("DELETE", "/private/v1/cancelOrders", json_body={"symbol": symbol})

    async def wait_for_position(
        self,
        *,
        symbol: str,
        expected_side: str,
        expected_size: float,
        timeout: float = 8.0,
    ) -> PositionSummary:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            summary = await self.fetch_position_summary(symbol)
            if expected_side == "FLAT":
                if summary.size == 0:
                    return summary
            else:
                if summary.side == expected_side and summary.size >= expected_size - 1e-8:
                    return summary
            await asyncio.sleep(0.5)
        return await self.fetch_position_summary(symbol)
