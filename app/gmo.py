from __future__ import annotations

import asyncio
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Awaitable, Callable, Dict, Optional

import pybotters
from loguru import logger

try:  # pragma: no cover - optional import guard
    from pydantic import BaseModel
except Exception:  # pragma: no cover - fallback for runtime environments
    class BaseModel:  # type: ignore[override]
        pass


def _to_jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        value = value.dict(by_alias=True)
    if isinstance(value, dict):
        return {key: _to_jsonable(val) for key, val in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_jsonable(item) for item in value]
    if isinstance(value, Decimal):
        return str(value)
    return value


def _assert_jsonable(value: Any, ctx: str = "payload") -> None:
    import json

    json.dumps(_to_jsonable(value))


def _format_decimal(value: float | Decimal) -> str:
    return str(Decimal(str(value)))


@dataclass
class OrderResult:
    success: bool
    status_code: int
    data: Dict[str, Any]
    message_code: Optional[str]
    message_string: Optional[str]


class GMOBroker:
    BASE_URL = "https://api.coin.z.com"

    def __init__(self, client: pybotters.Client) -> None:
        self._client = client
        self._execution_event = asyncio.Event()
        self._latest_execution: Optional[Dict[str, Any]] = None
        self._position_summary: Dict[str, Any] = {}
        self.retry_entry = 0
        self.retry_close = 0
        self._ws_connected = False

    @property
    def ws_connected(self) -> bool:
        return self._ws_connected

    @property
    def position_summary(self) -> Dict[str, Any]:
        return self._position_summary

    async def on_ws_connected(self) -> None:
        self._ws_connected = True

    async def on_ws_disconnected(self) -> None:
        self._ws_connected = False

    async def handle_ws_message(self, message: Dict[str, Any]) -> None:
        channel = message.get("channel")
        data = message.get("data")
        if channel == "executionEvents" and isinstance(data, dict):
            self._latest_execution = message
            self._execution_event.set()
        elif channel == "positionSummaryEvents" and isinstance(data, dict):
            symbol = data.get("symbol")
            if symbol:
                self._position_summary[symbol] = {"data": [data]}

    async def close(self) -> None:
        self._execution_event.set()
        self._ws_connected = False

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
        async with self._client.get("/private/v1/openPositions", params={"symbol": symbol}) as resp:
            status_code = resp.status
            try:
                data = await resp.json()
            except Exception:
                text_body = await resp.text()
                logger.error(
                    "Failed to parse positions response",
                    symbol=symbol,
                    http_status=status_code,
                    body_preview=text_body[:200],
                )
                raise RuntimeError("Failed to fetch positions")
            if status_code >= 400:
                messages = []
                if isinstance(data, dict):
                    messages = data.get("messages") or []
                message_code = messages[0].get("message_code") if messages else None
                message_string = messages[0].get("message_string") if messages else None
                logger.error(
                    "GMO openPositions failed",
                    symbol=symbol,
                    http_status=status_code,
                    message_code=message_code,
                    message_string=message_string,
                )
                raise RuntimeError(message_string or "Failed to fetch positions")
            if isinstance(data, dict):
                self._position_summary[symbol] = data
            return data

    def _should_retry(self, status_code: int) -> bool:
        return status_code >= 500 or status_code == 429

    async def _place_order(self, endpoint: str, payload: Dict[str, Any], context: str) -> OrderResult:
        delays = [0.0, 0.5, 1.0, 2.0]
        message_code: Optional[str] = None
        message_string: Optional[str] = None
        last_data: Dict[str, Any] = {}
        last_status = 0
        payload = _to_jsonable(payload)
        _assert_jsonable(payload, ctx=f"{context}-payload")
        for delay in delays:
            if delay:
                await asyncio.sleep(delay)
            async with self._client.post(endpoint, json=payload) as resp:
                status_code = resp.status
                last_status = status_code
                try:
                    data = await resp.json()
                except Exception:
                    text_body = await resp.text()
                    data = {"raw": text_body}
                if not isinstance(data, dict):
                    data = {"data": data}
                last_data = data
                messages = []
                if isinstance(data, dict):
                    messages = data.get("messages") or []
                resp_message_code: Optional[str] = None
                resp_message_string: Optional[str] = None
                if messages:
                    resp_message_code = messages[0].get("message_code")
                    resp_message_string = messages[0].get("message_string")
                    message_code = resp_message_code
                    message_string = resp_message_string
                logger_level = logger.info if status_code < 400 else logger.warning
                logger_level(
                    "GMO REST response",
                    endpoint=endpoint,
                    context=context,
                    http_status=status_code,
                    message_code=resp_message_code,
                    message_string=resp_message_string,
                )
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
            "side": side.upper(),
            "executionType": "MARKET",
            "size": _format_decimal(size),
        }
        return await self._place_order("/private/v1/order", payload, "entry")

    async def place_close(self, symbol: str, side: str, size: float) -> OrderResult:
        payload = {
            "symbol": symbol,
            "side": side.upper(),
            "executionType": "MARKET",
            "size": _format_decimal(size),
        }
        return await self._place_order("/private/v1/order", payload, "close")

    async def close_bulk(self, symbol: str) -> OrderResult:
        payload = {"symbol": symbol}
        return await self._place_order("/private/v1/closeBulkOrder", payload, "close")


async def _ws_worker(
    pyb_client: pybotters.Client,
    token: str,
    on_message: Callable[[Dict[str, Any]], Awaitable[None]],
    *,
    stop_event: Optional[asyncio.Event] = None,
    on_connect: Optional[Callable[[], Awaitable[None]]] = None,
    on_disconnect: Optional[Callable[[], Awaitable[None]]] = None,
) -> None:
    urls = [
        f"wss://api.coin.z.com/ws/private/v1?token={token}",
        f"wss://api.coin.z.com/ws/private?token={token}",
    ]
    subscribe = {
        "command": "subscribe",
        "channel": [
            {"name": "executionEvents"},
            {"name": "positionSummaryEvents"},
        ],
    }
    backoff = 1.0
    while True:
        if stop_event and stop_event.is_set():
            logger.info("WS stop requested")
            return
        for url in urls:
            if stop_event and stop_event.is_set():
                logger.info("WS stop requested")
                return
            try:
                async with pyb_client.ws_connect(url) as ws:
                    await ws.send_json(subscribe)
                    logger.info("WS connected: {}", url)
                    if on_connect:
                        await on_connect()
                    backoff = 1.0
                    async for msg in ws:
                        if stop_event and stop_event.is_set():
                            await ws.close()
                            break
                        msg_type = getattr(msg.type, "name", str(msg.type))
                        if msg_type == "TEXT":
                            try:
                                data = msg.json()
                            except Exception as exc:  # pragma: no cover - defensive
                                logger.warning("WS message parse error", error=str(exc))
                                continue
                            try:
                                await on_message(data)
                            except Exception as exc:  # pragma: no cover - defensive
                                logger.warning("WS on_message error", error=str(exc))
                        elif msg_type in ("CLOSE", "CLOSED"):
                            raise ConnectionError("WS closed by server")
                if on_disconnect:
                    await on_disconnect()
            except asyncio.CancelledError:
                if on_disconnect:
                    await on_disconnect()
                raise
            except Exception as exc:
                if on_disconnect:
                    await on_disconnect()
                logger.warning("WS connection dropped", url=url, error=str(exc))
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 15.0)
                continue
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 15.0)

