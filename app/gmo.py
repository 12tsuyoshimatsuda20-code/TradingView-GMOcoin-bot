from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Dict, Optional

from aiohttp import WSMsgType
import pybotters
from loguru import logger


@dataclass
class OrderResult:
    success: bool
    status_code: int
    data: Dict[str, Any]
    message_code: Optional[str]
    message_string: Optional[str]


class GMOBroker:
    BASE_URL = "https://api.coin.z.com"
    WS_URL = "wss://api.coin.z.com/ws/private/v1"

    def __init__(self, api_key: str, api_secret: str) -> None:
        self._api_key = api_key
        self._api_secret = api_secret
        self._client_cm: Optional[pybotters.Client] = None
        self._client: Optional[pybotters.Client] = None
        self._ws_task: Optional[asyncio.Task] = None
        self._ws_connected = asyncio.Event()
        self._ws_stop = asyncio.Event()
        self._execution_event = asyncio.Event()
        self._latest_execution: Optional[Dict[str, Any]] = None
        self._position_summary: Dict[str, Any] = {}
        self.retry_entry = 0
        self.retry_close = 0

    @property
    def ws_connected(self) -> bool:
        return self._ws_connected.is_set()

    @property
    def position_summary(self) -> Dict[str, Any]:
        return self._position_summary

    async def connect(self) -> None:
        if self._client is not None:
            return
        self._client_cm = pybotters.Client(
            apis={"gmocoin": [self._api_key, self._api_secret]},
            base_url=self.BASE_URL,
        )
        self._client = await self._client_cm.__aenter__()
        logger.info("GMOBroker client connected")
        self._ws_task = asyncio.create_task(self._ws_worker())

    async def close(self) -> None:
        self._ws_stop.set()
        if self._ws_task:
            await self._ws_task
        if self._client_cm is not None:
            await self._client_cm.__aexit__(None, None, None)
            self._client_cm = None
            self._client = None
        logger.info("GMOBroker client closed")

    async def _ws_worker(self) -> None:
        if self._client is None:
            return
        while not self._ws_stop.is_set():
            try:
                async with self._client.ws_connect(self.WS_URL) as ws:
                    await ws.send_json(
                        {
                            "command": "subscribe",
                            "channel": "executionEvents",
                            "symbol": "BTC_JPY",
                        }
                    )
                    await ws.send_json(
                        {
                            "command": "subscribe",
                            "channel": "positionSummaryEvents",
                            "symbol": "BTC_JPY",
                        }
                    )
                    self._ws_connected.set()
                    async for msg in ws:
                        if self._ws_stop.is_set():
                            await ws.close()
                            break
                        if msg.type != WSMsgType.TEXT:
                            continue
                        data = msg.json()
                        channel = data.get("channel")
                        if channel == "executionEvents":
                            self._latest_execution = data
                            self._execution_event.set()
                        elif channel == "positionSummaryEvents":
                            summary = data.get("data", {})
                            symbol = summary.get("symbol")
                            if symbol:
                                self._position_summary[symbol] = summary
            except Exception as exc:
                self._ws_connected.clear()
                logger.warning("WS connection dropped", error=str(exc))
                await asyncio.sleep(3)
            else:
                self._ws_connected.clear()

    async def wait_for_execution(self, order_id: str, timeout: float = 3.0) -> Optional[Dict[str, Any]]:
        try:
            while True:
                self._execution_event.clear()
                await asyncio.wait_for(self._execution_event.wait(), timeout=timeout)
                if self._latest_execution and self._latest_execution.get("data"):
                    data = self._latest_execution["data"]
                    if data.get("orderId") == order_id:
                        return data
        except asyncio.TimeoutError:
            return None

    async def fetch_positions(self, symbol: str) -> Dict[str, Any]:
        if self._client is None:
            raise RuntimeError("GMOBroker client not connected")
        async with self._client.get(
            "/private/v1/openPositions", params={"symbol": symbol}
        ) as resp:
            data = await resp.json()
            self._position_summary[symbol] = data
            return data

    def _should_retry(self, status_code: int) -> bool:
        return status_code >= 500 or status_code == 429

    async def _place_order(self, endpoint: str, payload: Dict[str, Any], context: str) -> OrderResult:
        if self._client is None:
            raise RuntimeError("GMOBroker client not connected")
        delays = [0.0, 0.5, 1.0, 2.0]
        message_code: Optional[str] = None
        message_string: Optional[str] = None
        last_data: Dict[str, Any] = {}
        last_status = 0
        for delay in delays:
            if delay:
                await asyncio.sleep(delay)
            async with self._client.post(endpoint, json=payload) as resp:
                status_code = resp.status
                last_status = status_code
                data = await resp.json()
                last_data = data
                messages = data.get("messages") or []
                if messages:
                    message_code = messages[0].get("message_code")
                    message_string = messages[0].get("message_string")
                if status_code < 400:
                    return OrderResult(True, status_code, data, message_code, message_string)
                if not self._should_retry(status_code):
                    return OrderResult(False, status_code, data, message_code, message_string)
                if context == "close":
                    self.retry_close += 1
                else:
                    self.retry_entry += 1
        return OrderResult(False, last_status or 500, last_data, message_code, message_string)

    async def place_entry(self, symbol: str, side: str, size: float) -> OrderResult:
        payload = {
            "symbol": symbol,
            "side": side,
            "executionType": "MARKET",
            "size": size,
        }
        return await self._place_order("/private/v1/order", payload, "entry")

    async def place_close(self, symbol: str, side: str, size: float) -> OrderResult:
        payload = {
            "symbol": symbol,
            "side": side,
            "executionType": "MARKET",
            "size": size,
        }
        return await self._place_order("/private/v1/order", payload, "close")

    async def close_bulk(self, symbol: str) -> OrderResult:
        payload = {"symbol": symbol}
        return await self._place_order("/private/v1/closeBulkOrder", payload, "close")

