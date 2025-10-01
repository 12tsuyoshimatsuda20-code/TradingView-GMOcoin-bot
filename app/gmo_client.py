"""Thin wrapper around pybotters for GMO Coin REST endpoints."""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

import pybotters

from .logger import get_logger


class GMOAPIError(RuntimeError):
    """Raised when GMO Coin responds with an error."""


class GMOClient:
    """Small helper for the subset of GMO Coin endpoints we require."""

    def __init__(self, client: pybotters.Client, *, api_base: str) -> None:
        self._client = client
        self._api_base = api_base.rstrip("/")
        self._log = get_logger(__name__)
        self._max_retries = 3
        self._backoff_base = 0.5

    async def submit_market_entry(self, *, symbol: str, side: str, size: str) -> Dict[str, Any]:
        payload = {
            "symbol": symbol,
            "side": side,
            "executionType": "MARKET",
            "size": size,
        }
        # Signature target: timestamp + method + path + body(JSON string)
        return await self._request("post", "/private/v1/order", json=payload)

    async def fetch_open_positions(self, *, symbol: str) -> List[Dict[str, Any]]:
        params = {"symbol": symbol, "page": 1, "count": 100}
        # Signature target: timestamp + method + path
        data = await self._request("get", "/private/v1/openPositions", params=params)
        return data.get("data", [])

    async def submit_close_bulk_order(
        self, *, symbol: str, side: str, size: str
    ) -> Dict[str, Any]:
        payload = {
            "symbol": symbol,
            "side": side,
            "executionType": "MARKET",
            "size": size,
        }
        # Signature target: timestamp + method + path + body(JSON string)
        return await self._request("post", "/private/v1/closeBulkOrder", json=payload)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        url = f"{self._api_base}{path}"
        func = getattr(self._client, method.lower())
        last_error: Optional[Exception] = None
        for attempt in range(1, self._max_retries + 1):
            response = None
            try:
                response = await func(url, params=params, json=json, sign=True)
                if response.status >= 500:
                    text = await response.text()
                    self._log.warning(
                        "GMO API server error",
                        extra={
                            "url": path,
                            "status": response.status,
                            "body": text[:300],
                            "attempt": attempt,
                        },
                    )
                    last_error = GMOAPIError(f"Server error {response.status}")
                elif response.status >= 400:
                    text = await response.text()
                    self._log.error(
                        "GMO API client error",
                        extra={
                            "url": path,
                            "status": response.status,
                            "body": text[:300],
                        },
                    )
                    raise GMOAPIError(f"HTTP {response.status}: {text}")
                else:
                    data = await response.json()
                    self._log.debug(
                        "GMO API response",
                        extra={"url": path, "status": response.status},
                    )
                    return data
            except GMOAPIError:
                raise
            except Exception as exc:  # pragma: no cover - network issues
                self._log.warning(
                    "GMO API request failed",
                    extra={"url": path, "attempt": attempt, "error": str(exc)},
                )
                last_error = exc
            finally:
                if response is not None:
                    await response.release()
            if attempt < self._max_retries:
                await asyncio.sleep(self._backoff_base * (2 ** (attempt - 1)))
        if last_error:
            raise GMOAPIError(str(last_error))
        raise GMOAPIError("Unknown GMO API error")
