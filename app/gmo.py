from __future__ import annotations

import asyncio
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from typing import Iterable, List, Optional

import pybotters

from .config import settings
from .logging import get_logger

logger = get_logger()


class GMOAPIError(RuntimeError):
    def __init__(self, message: str, *, http_status: int | None = None, code: str | None = None, detail: str | None = None, payload: dict | None = None) -> None:
        super().__init__(message)
        self.http_status = http_status
        self.code = code
        self.detail = detail
        self.payload = payload or {}

    def to_dict(self) -> dict:
        return {
            "message": str(self),
            "http_status": self.http_status,
            "code": self.code,
            "detail": self.detail,
            "payload": self.payload,
        }


@dataclass(slots=True)
class Position:
    position_id: str
    symbol: str
    side: str
    size: Decimal
    price: Decimal


@dataclass(slots=True)
class PositionSummary:
    side: Optional[str]
    size: Decimal
    positions: List[Position]

    @property
    def is_flat(self) -> bool:
        return self.size == 0


def _format_decimal(value: Decimal) -> str:
    quantized = value.quantize(settings.qty_step, rounding=ROUND_DOWN)
    normalized = quantized.normalize()
    return format(normalized, "f") if normalized != normalized.to_integral() else format(normalized, "f")


class GMOCoinClient:
    def __init__(self, client: pybotters.Client, *, symbol: str = "BTC_JPY") -> None:
        self._client = client
        self._symbol = symbol

    async def _request(self, method: str, path: str, *, params: dict | None = None, json: dict | None = None) -> dict:
        attempt = 0
        last_error: GMOAPIError | None = None
        while attempt <= settings.retry_limit:
            response = await self._client.request(method, path, params=params, json=json)
            payload = await response.json()
            http_status = response.status
            status_code = payload.get("status")
            message_code = payload.get("data", {}).get("code") or payload.get("message_code")
            message_string = payload.get("data", {}).get("message") or payload.get("message_string")
            if 200 <= http_status < 300 and status_code == 0:
                return payload
            if http_status in {429} or http_status >= 500:
                last_error = GMOAPIError(
                    "temporary GMO API error",
                    http_status=http_status,
                    code=message_code,
                    detail=message_string,
                    payload=payload,
                )
                await asyncio.sleep(0.5 * (2 ** attempt))
                attempt += 1
                continue
            raise GMOAPIError(
                "GMO API request failed",
                http_status=http_status,
                code=message_code,
                detail=message_string,
                payload=payload,
            )
        assert last_error is not None
        raise last_error

    async def get_open_positions(self) -> PositionSummary:
        params = {"symbol": self._symbol, "page": 1, "count": 100}
        payload = await self._request("GET", "/private/v1/openPositions", params=params)
        positions = []
        for item in payload.get("data", {}).get("list", []):
            try:
                size = Decimal(item["size"])
                price = Decimal(item.get("price", "0"))
            except (KeyError, ValueError):
                continue
            position = Position(
                position_id=item["positionId"],
                symbol=item["symbol"],
                side=item["side"],
                size=size,
                price=price,
            )
            positions.append(position)
        side = None
        total_size = Decimal("0")
        for position in positions:
            if total_size == 0:
                side = position.side
            total_size += position.size
        return PositionSummary(side=side, size=total_size, positions=positions)

    async def submit_entry(self, *, side: str, size: Decimal) -> dict:
        body = {
            "symbol": self._symbol,
            "side": side,
            "executionType": "MARKET",
            "size": _format_decimal(size),
        }
        result = await self._request("POST", "/private/v1/order", json=body)
        return result

    async def submit_close(self, positions: Iterable[Position]) -> dict:
        settle = [
            {
                "positionId": position.position_id,
                "side": position.side,
                "size": _format_decimal(position.size),
            }
            for position in positions
        ]
        body = {
            "symbol": self._symbol,
            "executionType": "MARKET",
            "settlePosition": settle,
        }
        result = await self._request("POST", "/private/v1/closeBulkOrder", json=body)
        return result

    async def close_all(self) -> dict | None:
        summary = await self.get_open_positions()
        if summary.is_flat:
            return None
        return await self.submit_close(summary.positions)
