"""Minimal GMO Coin REST helper built on top of pybotters."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from typing import Any, Dict, Optional

import aiohttp
from loguru import logger
import pybotters

API_BASE = "https://api.coin.z.com"
PRIVATE_BASE = f"{API_BASE}/private/v1"

ENDPOINTS = {
    "positions": f"{PRIVATE_BASE}/openPositions",
    "order": f"{PRIVATE_BASE}/order",
    "close": f"{PRIVATE_BASE}/closeOrder",
}

# エラーのうち settle_qty が過大な場合に返ると想定されるコード群
SETTLE_QTY_ERROR_CODES = {
    "ERR-7201",  # 指定数量が保有数量を超過
    "ERR-7202",
    "ERR-7203",
}

RETRY_METRICS: Dict[str, int] = {
    "positions": 0,
    "order": 0,
    "close": 0,
}


@dataclass
class GmoAPIError(Exception):
    """Raised when the GMO Coin API returns an application level error."""

    status_code: int
    message_code: Optional[str]
    message_string: Optional[str]
    payload: Dict[str, Any]
    endpoint: str

    @property
    def is_settle_qty_error(self) -> bool:
        if self.message_code and self.message_code in SETTLE_QTY_ERROR_CODES:
            return True
        if self.message_string and "settable_qty" in self.message_string:
            return True
        if self.message_string and "settle_qty" in self.message_string:
            return True
        return False

    def __str__(self) -> str:
        return (
            f"GMO API error endpoint={self.endpoint} status={self.status_code} "
            f"code={self.message_code} message={self.message_string}"
        )


async def _request(
    client: pybotters.Client,
    method: str,
    endpoint_key: str,
    *,
    params: Optional[Dict[str, Any]] = None,
    json: Optional[Dict[str, Any]] = None,
    max_attempts: int = 3,
) -> Dict[str, Any]:
    """Perform a REST request with retries on rate limiting / server errors."""

    url = ENDPOINTS[endpoint_key]
    delay = 0.5
    last_exception: Optional[BaseException] = None
    for attempt in range(1, max_attempts + 1):
        try:
            response = await getattr(client, method)(
                url,
                params=params,
                json=json,
                timeout=aiohttp.ClientTimeout(total=5),
            )
            status = response.status
            data = await response.json()
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            last_exception = exc
            logger.warning(
                "HTTP exception endpoint={} attempt={} err={}",
                endpoint_key,
                attempt,
                exc,
            )
            if attempt == max_attempts:
                raise
        else:
            messages = data.get("messages") or []
            first_message = messages[0] if messages else {}
            message_code = first_message.get("message_code")
            message_string = first_message.get("message_string")

            # GMO Coin は status==0 が成功。それ以外はエラー。
            if status in (429,) or status >= 500:
                last_exception = GmoAPIError(
                    status,
                    message_code,
                    message_string,
                    data,
                    endpoint_key,
                )
                logger.warning(
                    "Retry endpoint={} status={} code={} msg={}",
                    endpoint_key,
                    status,
                    message_code,
                    message_string,
                )
            elif data.get("status") in (0, "0"):
                return data
            else:
                # アプリケーションエラー
                raise GmoAPIError(status, message_code, message_string, data, endpoint_key)

        # リトライ待機
        if attempt < max_attempts:
            RETRY_METRICS[endpoint_key] = RETRY_METRICS.get(endpoint_key, 0) + 1
            await asyncio.sleep(delay)
            delay *= 2

    assert last_exception is not None
    if isinstance(last_exception, Exception):
        raise last_exception
    raise RuntimeError("Unknown error during GMO Coin request")


def round_qty(size: float, step: float) -> float:
    """Round down the size to the nearest step."""

    if step <= 0:
        raise ValueError("step must be positive")
    decimal_size = Decimal(str(size))
    decimal_step = Decimal(str(step))
    rounded = (decimal_size / decimal_step).to_integral_value(rounding=ROUND_DOWN)
    return float(rounded * decimal_step)


async def get_positions(client: pybotters.Client, symbol: str) -> Dict[str, Any]:
    """Return the total position size and side for the specified symbol."""

    data = await _request(client, "get", "positions", params={"symbol": symbol})
    total_qty = Decimal("0")
    side: Optional[str] = None

    for position in data.get("data", []) or []:
        if position.get("symbol") != symbol:
            continue
        size = Decimal(str(position.get("size", "0")))
        if size <= 0:
            continue
        total_qty += size
        side = position.get("side")

    return {"qty": float(total_qty), "side": side}


async def place_entry_order(
    client: pybotters.Client,
    symbol: str,
    side: str,
    size: float,
) -> Dict[str, Any]:
    payload = {
        "symbol": symbol,
        "side": side,
        "executionType": "MARKET",
        "size": f"{Decimal(str(size)):.8f}",
    }
    return await _request(client, "post", "order", json=payload)


async def place_close_order(
    client: pybotters.Client,
    symbol: str,
    side: str,
    size: float,
) -> Dict[str, Any]:
    payload = {
        "symbol": symbol,
        "side": side,
        "executionType": "MARKET",
        "size": f"{Decimal(str(size)):.8f}",
    }
    return await _request(client, "post", "close", json=payload)
