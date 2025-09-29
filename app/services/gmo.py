from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Dict, Optional

import pybotters
from loguru import logger

from app.config import Settings
from app.utils import utcnow


@dataclass
class Position:
    size: float
    side: Optional[str]


class GmoCoinService:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client: Optional[pybotters.Client] = None
        self._lock = asyncio.Lock()
        self._retry_stats: Dict[str, int] = {"rest_retries": 0}
        self._ws_connected = False

    @property
    def retry_stats(self) -> Dict[str, int]:
        return dict(self._retry_stats)

    @property
    def websocket_connected(self) -> bool:
        return self._ws_connected

    async def start(self) -> None:
        if self._client is not None:
            return
        self._client = pybotters.Client(
            apis={"gmocoin": [self._settings.gmo_api_key, self._settings.gmo_api_secret]},
            base_url=self._settings.gmo_base_url,
        )
        asyncio.create_task(self._noop_ws_guard())

    async def stop(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None
        self._ws_connected = False

    async def _noop_ws_guard(self) -> None:
        # Placeholder guard to mark websocket status as unavailable
        logger.info("WS monitor placeholder activated; no live WS connection established")
        self._ws_connected = False

    async def get_position(self, symbol: str) -> Position:
        response = await self._request("get", "/private/v1/openPositions", params={"symbol": symbol})
        data = response.get("data", [])
        total_size = 0.0
        side: Optional[str] = None
        for item in data:
            size = float(item.get("size", 0))
            if size <= 0:
                continue
            total_size += size
            side = item.get("side")
        logger.debug("Fetched open position: size=%s side=%s", total_size, side)
        return Position(size=total_size, side=side)

    async def place_entry(self, symbol: str, side: str, size: float) -> Dict[str, Any]:
        payload = {
            "symbol": symbol,
            "side": side,
            "executionType": "MARKET",
            "size": f"{size:.8f}",
        }
        response = await self._request("post", "/private/v1/order", json=payload)
        await self._wait_for_fill(symbol, expected_size=size, target_side=side, timeout=10)
        return response

    async def close_position(self, symbol: str, side: str, size: float) -> Dict[str, Any]:
        payload = {
            "symbol": symbol,
            "side": side,
            "executionType": "MARKET",
            "size": f"{size:.8f}",
        }
        response = await self._request("post", "/private/v1/closeBulkOrder", json=payload)
        await self._wait_for_flat(symbol, timeout=10)
        return response

    async def _wait_for_fill(self, symbol: str, expected_size: float, target_side: str, timeout: int) -> None:
        deadline = utcnow() + timedelta(seconds=timeout)
        while utcnow() < deadline:
            position = await self.get_position(symbol)
            if position.size >= expected_size and position.side == target_side:
                return
            await asyncio.sleep(1)
        logger.warning("Fill confirmation timeout for entry order")

    async def _wait_for_flat(self, symbol: str, timeout: int) -> None:
        deadline = utcnow() + timedelta(seconds=timeout)
        while utcnow() < deadline:
            position = await self.get_position(symbol)
            if position.size == 0:
                return
            await asyncio.sleep(1)
        logger.warning("Flat confirmation timeout for close order")

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json: Optional[Dict[str, Any]] = None,
        retries: int = 3,
    ) -> Dict[str, Any]:
        if self._client is None:
            raise RuntimeError("GMO client not started")
        attempt = 0
        wait = 0.5
        last_error: Optional[Exception] = None
        while attempt < retries:
            attempt += 1
            try:
                request = getattr(self._client, method)
                should_retry = False
                async with self._lock:
                    async with request(path, params=params, json=json) as response:
                        if response.status == 429 or response.status >= 500:
                            text = await response.text()
                            logger.warning(
                                "GMO API transient error status=%s body=%s attempt=%s",
                                response.status,
                                text,
                                attempt,
                            )
                            if attempt < retries:
                                should_retry = True
                            else:
                                response.raise_for_status()
                        elif 400 <= response.status < 500:
                            text = await response.text()
                            logger.error(
                                "GMO API client error status=%s body=%s", response.status, text
                            )
                            response.raise_for_status()
                        if should_retry:
                            data = None
                        else:
                            data = await response.json()
                if should_retry:
                    self._retry_stats["rest_retries"] += 1
                    await asyncio.sleep(wait)
                    wait *= 2
                    continue
                logger.debug("GMO API response path=%s data=%s", path, data)
                return data
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                logger.exception("GMO API request failed on attempt %s: %s", attempt, exc)
                if attempt >= retries:
                    break
                self._retry_stats["rest_retries"] += 1
                await asyncio.sleep(wait)
                wait *= 2
        if last_error:
            raise last_error
        raise RuntimeError("Unknown GMO request failure")
