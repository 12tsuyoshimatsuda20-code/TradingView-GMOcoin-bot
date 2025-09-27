from __future__ import annotations

from typing import Any, Dict, Optional

import pybotters
from loguru import logger
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_attempt, wait_exponential


class GMOCoinAPIError(Exception):
    """Raised when GMO Coin API returns an error response."""


class GMOCoinClient:
    base_url = "https://api.coin.z.com/private"

    def __init__(self, api_key: str, api_secret: str) -> None:
        self._client = pybotters.Client(apis={"gmocoin": (api_key, api_secret)})

    async def close(self) -> None:
        await self._client.close()

    async def _request(
        self,
        method: str,
        endpoint: str,
        payload: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        url = f"{self.base_url}{endpoint}"

        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(3),
            wait=wait_exponential(multiplier=0.5, min=0.5, max=2.0),
            retry=retry_if_exception_type(GMOCoinAPIError),
            reraise=True,
        ):
            with attempt:
                logger.debug(
                    "GMO request start",
                    method=method,
                    url=url,
                    payload=payload,
                    params=params,
                )
                async with self._client.request(method, url, params=params, json=payload) as response:
                    data = await response.json()
                    logger.bind(endpoint=endpoint).info(
                        "GMO response",
                        http_status=response.status,
                        status=data.get("status"),
                        messages=data.get("messages"),
                        responsetime=data.get("responsetime"),
                    )
                    if response.status >= 400 or data.get("status") not in (None, 0):
                        raise GMOCoinAPIError(str(data))
                    return data

        raise GMOCoinAPIError("Maximum retry attempts exceeded")

    async def get_open_positions(self, symbol: str) -> Dict[str, Any]:
        params = {"symbol": symbol}
        return await self._request("GET", "/v1/openPositions", payload=None, params=params)

    async def place_market_entry(self, symbol: str, side: str, size: str) -> Dict[str, Any]:
        payload = {
            "symbol": symbol,
            "side": side,
            "executionType": "MARKET",
            "size": size,
        }
        return await self._request("POST", "/v1/order", payload=payload)

    async def place_market_close_all(self, symbol: str) -> Dict[str, Any]:
        payload = {
            "symbol": symbol,
        }
        return await self._request("POST", "/v1/closeBulkOrder", payload=payload)
