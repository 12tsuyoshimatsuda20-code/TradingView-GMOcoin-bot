from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Literal, Optional, Sequence

import pybotters


def _format_size(size: float) -> str:
    text = format(size, ".8f")
    text = text.rstrip("0").rstrip(".")
    return text or "0"


class GMOCoinError(Exception):
    def __init__(self, message: str, *, status: Optional[int] = None, code: Optional[str] = None, retryable: bool = False, data: Optional[dict] = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.retryable = retryable
        self.data = data or {}


@dataclass
class PositionSummary:
    symbol: str
    net_side: Optional[Literal["BUY", "SELL"]]
    net_qty: float
    positions: List[dict]

    @property
    def is_flat(self) -> bool:
        return self.net_qty == 0 or self.net_side is None


class GMOCoinClient:
    BASE_URL = "https://api.coin.z.com"

    def __init__(self, api_key: str, api_secret: str, *, base_url: Optional[str] = None) -> None:
        self._client = pybotters.Client(
            apis={"gmocoin": [api_key, api_secret]},
            base_url=base_url or self.BASE_URL,
        )

    async def close(self) -> None:
        await self._client.close()

    async def _request_with_retry(
        self,
        method: str,
        url: str,
        *,
        params: Optional[dict] = None,
        json: Optional[dict] = None,
        name: str,
    ) -> dict:
        delays = [0.5, 1.0, 2.0]
        last_err: Optional[GMOCoinError] = None
        for attempt, delay in enumerate(delays, start=1):
            try:
                async with self._client.request(method, url, params=params, json=json, timeout=30) as resp:
                    data = await resp.json()
            except asyncio.TimeoutError as exc:
                last_err = GMOCoinError(
                    f"{name} timeout",
                    status=None,
                    code="timeout",
                    retryable=True,
                    data={"attempt": attempt},
                )
            else:
                status = resp.status
                api_status = data.get("status")
                if status in (429,) or status >= 500:
                    last_err = GMOCoinError(
                        f"{name} http_error",
                        status=status,
                        code=str(api_status),
                        retryable=True,
                        data={"attempt": attempt, "body": data},
                    )
                elif status >= 400:
                    raise GMOCoinError(
                        f"{name} client_error",
                        status=status,
                        code=str(api_status),
                        retryable=False,
                        data={"body": data},
                    )
                elif api_status not in (0, "0", None, "success"):
                    raise GMOCoinError(
                        f"{name} api_error",
                        status=status,
                        code=str(api_status),
                        retryable=False,
                        data={"body": data},
                    )
                else:
                    return data
            await asyncio.sleep(delay)
        assert last_err is not None
        raise last_err

    async def get_open_positions(self, symbol: str) -> List[dict]:
        data = await self._request_with_retry(
            "GET",
            "/private/v1/openPositions",
            params={"symbol": symbol, "page": 1, "count": 100},
            name="get_open_positions",
        )
        positions = data.get("data") or []
        return positions

    async def get_position_summary(self, symbol: str) -> PositionSummary:
        positions = await self.get_open_positions(symbol)
        long_qty = 0.0
        short_qty = 0.0
        for pos in positions:
            side = pos.get("side")
            qty = float(pos.get("size") or pos.get("positionSize") or 0.0)
            if side == "BUY":
                long_qty += qty
            elif side == "SELL":
                short_qty += qty
        net = long_qty - short_qty
        if math.isclose(net, 0.0, abs_tol=1e-9):
            net_side: Optional[Literal["BUY", "SELL"]] = None
            net_qty = 0.0
        elif net > 0:
            net_side = "BUY"
            net_qty = round(net, 8)
        else:
            net_side = "SELL"
            net_qty = round(abs(net), 8)
        return PositionSummary(symbol=symbol, net_side=net_side, net_qty=net_qty, positions=positions)

    async def place_market_order(
        self,
        symbol: str,
        side: Literal["BUY", "SELL"],
        size: float,
        *,
        settle_positions: Optional[Sequence[dict]] = None,
        time_in_force: str = "FAS",
    ) -> dict:
        payload: Dict[str, Any] = {
            "symbol": symbol,
            "side": side,
            "executionType": "MARKET",
            "timeInForce": time_in_force,
            "size": _format_size(size),
        }
        if settle_positions:
            payload["settlePositionPositionId"] = [
                {
                    "positionId": p["positionId"],
                    "settlePositionSize": _format_size(float(p["size"])),
                }
                for p in settle_positions
                if p.get("positionId") and float(p.get("size", 0)) > 0
            ]
        return await self._request_with_retry(
            "POST",
            "/private/v1/order",
            json=payload,
            name="place_market_order",
        )

    async def wait_until_position_matches(
        self,
        symbol: str,
        expected_side: Optional[Literal["BUY", "SELL"]],
        expected_qty: float,
        *,
        timeout: float = 10.0,
        poll_interval: float = 0.5,
    ) -> PositionSummary:
        deadline = time.monotonic() + timeout
        last_summary: Optional[PositionSummary] = None
        while time.monotonic() < deadline:
            summary = await self.get_position_summary(symbol)
            last_summary = summary
            if expected_side is None:
                if summary.is_flat:
                    return summary
            else:
                if summary.net_side == expected_side and math.isclose(summary.net_qty, expected_qty, rel_tol=1e-6, abs_tol=1e-6):
                    return summary
            await asyncio.sleep(poll_interval)
        return last_summary or PositionSummary(symbol=symbol, net_side=None, net_qty=0.0, positions=[])

