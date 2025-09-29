from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import pybotters
from loguru import logger

from storage import Storage
from utils import async_retry, utcnow

RETRY_STATUS = {429, 500, 502, 503, 504}
BASE_URL = "https://api.coin.z.com"
PRIVATE_WS_URL = "wss://api.coin.z.com/ws/private/v1"


class GMOClientError(Exception):
    def __init__(self, message: str, *, http_status: int | None = None, payload: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.http_status = http_status
        self.payload = payload or {}


@dataclass
class PositionState:
    side: Optional[str] = None
    qty: float = 0.0


class GMOClient:
    def __init__(self, api_key: str, api_secret: str, storage: Storage) -> None:
        self.api_key = api_key
        self.api_secret = api_secret
        self.storage = storage
        self.client: Optional[pybotters.Client] = None
        self._ws_task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()
        self._position = PositionState()

    async def start(self) -> None:
        if not self.api_key or not self.api_secret:
            raise RuntimeError("GMO API credentials are not configured")
        logger.info("initializing pybotters client")
        self.client = pybotters.Client(
            apis={"gmocoin": (self.api_key, self.api_secret)},
            base_url=BASE_URL,
        )
        await self.refresh_position()
        self._ws_task = asyncio.create_task(self._ws_loop(), name="gmo-ws-loop")

    async def close(self) -> None:
        self._stop_event.set()
        if self._ws_task:
            self._ws_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._ws_task
            self._ws_task = None
        if self.client:
            await self.client.close()
            self.client = None

    async def refresh_position(self) -> PositionState:
        positions = await self.get_open_positions("BTC_JPY")
        state = self._aggregate_positions(positions)
        self._position = state
        await self.storage.update_status(
            position_side=state.side,
            position_qty=state.qty,
        )
        return state

    async def get_open_positions(self, symbol: str) -> List[Dict[str, Any]]:
        response = await self._request("get", "/private/v1/openPositions", params={"symbol": symbol})
        return response.get("data", [])

    async def market_entry(self, symbol: str, side: str, size: float) -> Dict[str, Any]:
        payload = {
            "symbol": symbol,
            "side": side,
            "executionType": "MARKET",
            "size": f"{size:.8f}",
        }
        response = await self._request("post", "/private/v1/order", json=payload)
        await self.refresh_position()
        return response

    async def market_close_all(self, symbol: str) -> Dict[str, Any]:
        positions = await self.get_open_positions(symbol)
        state = self._aggregate_positions(positions)
        if not positions or state.qty <= 0:
            return {"status": 0, "message": "no position"}
        close_side = "SELL" if state.side == "BUY" else "BUY"
        remaining = state.qty
        settle: List[Dict[str, Any]] = []
        for pos in positions:
            try:
                position_id = int(pos.get("positionId"))
                size = float(pos.get("size") or pos.get("settleSize") or 0.0)
            except (TypeError, ValueError):
                continue
            qty = min(size, remaining)
            if qty <= 0:
                continue
            settle.append({"positionId": position_id, "size": f"{qty:.8f}"})
            remaining -= qty
            if remaining <= 1e-9:
                break
        if not settle:
            raise GMOClientError(
                "no settle positions",
                payload={"message_code": "NO_SETTLE", "message_string": "no settle positions"},
            )
        payload = {
            "symbol": symbol,
            "side": close_side,
            "executionType": "MARKET",
            "settlePosition": settle,
        }
        response = await self._request("post", "/private/v1/closeBulkOrder", json=payload)
        await self.refresh_position()
        return response

    async def _request(self, method: str, path: str, **kwargs: Any) -> Dict[str, Any]:
        if not self.client:
            raise RuntimeError("pybotters client is not initialized")

        async def operation() -> pybotters.client.Response:
            http_method = getattr(self.client, method.lower())
            return await http_method(path, **kwargs)

        async def on_retry(_: int, __: Exception) -> None:
            await self.storage.increment_retry("rest")

        response = await async_retry(
            operation,
            retries=3,
            retry_statuses=RETRY_STATUS,
            fatal_statuses={400, 401, 403, 404},
            on_retry=on_retry,
        )
        json_body = await response.json()
        if response.status >= 400 or json_body.get("status") not in (0, "0"):
            message_code = json_body.get("message_code")
            message_string = json_body.get("message_string") or json_body.get("data")
            error_payload = {
                "message_code": message_code,
                "message_string": message_string,
            }
            raise GMOClientError(
                "GMO API error",
                http_status=response.status,
                payload={**json_body, **error_payload},
            )
        return json_body

    def _aggregate_positions(self, positions: List[Dict[str, Any]]) -> PositionState:
        total_buy = 0.0
        total_sell = 0.0
        for pos in positions:
            side = pos.get("side")
            try:
                size = float(pos.get("size") or pos.get("settleSize") or 0.0)
            except (TypeError, ValueError):
                size = 0.0
            if side == "BUY":
                total_buy += size
            elif side == "SELL":
                total_sell += size
        if total_buy > total_sell:
            return PositionState(side="BUY", qty=round(total_buy - total_sell, 8))
        if total_sell > total_buy:
            return PositionState(side="SELL", qty=round(total_sell - total_buy, 8))
        return PositionState(side=None, qty=0.0)

    async def _ws_loop(self) -> None:
        if not self.client:
            return
        subscribe = [
            {"command": "subscribe", "channel": "positionSummaryEvents"},
            {"command": "subscribe", "channel": "executionEvents"},
        ]
        while not self._stop_event.is_set():
            try:
                logger.info("connecting to GMO private websocket")
                runner = await self.client.ws_connect(
                    PRIVATE_WS_URL,
                    send_json=subscribe,
                    hdlr_json=self._handle_ws_message,
                )
                await self.storage.update_status(ws_connected=True)
                stop_task = asyncio.create_task(self._stop_event.wait())
                runner_task = asyncio.create_task(runner.wait())
                done, pending = await asyncio.wait(
                    {stop_task, runner_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in pending:
                    task.cancel()
                for task in done:
                    with contextlib.suppress(Exception):
                        task.result()
            except asyncio.CancelledError:
                logger.info("websocket loop cancelled")
                break
            except Exception as exc:  # noqa: BLE001
                logger.warning("websocket reconnect scheduled", error=str(exc))
                await self.storage.update_status(ws_connected=False)
                await asyncio.sleep(5)
            finally:
                with contextlib.suppress(Exception):
                    if 'runner' in locals():
                        await runner.close()
                        del runner
        await self.storage.update_status(ws_connected=False)

    def _handle_ws_message(self, message: Dict[str, Any], ws=None) -> None:  # noqa: ANN001
        channel = message.get("channel")
        if channel == "positionSummaryEvents":
            data = message.get("data") or {}
            buy_size = float(data.get("buyPositionSize", 0) or 0)
            sell_size = float(data.get("sellPositionSize", 0) or 0)
            side = None
            qty = 0.0
            if buy_size > sell_size:
                side = "BUY"
                qty = buy_size - sell_size
            elif sell_size > buy_size:
                side = "SELL"
                qty = sell_size - buy_size
            self._position = PositionState(side=side, qty=qty)
            asyncio.create_task(
                self.storage.update_status(
                    position_side=side,
                    position_qty=qty,
                    ws_connected=True,
                )
            )
        elif channel == "executionEvents":
            data = message.get("data") or {}
            logger.debug("execution event", data=data, ts=str(utcnow()))

    @property
    def position(self) -> PositionState:
        return self._position
