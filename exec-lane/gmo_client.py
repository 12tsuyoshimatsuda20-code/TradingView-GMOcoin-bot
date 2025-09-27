from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pybotters
from loguru import logger

from .store import StatusStore


@dataclass
class OrderResult:
    order_id: str
    status: str
    data: Dict[str, Any]


@dataclass
class CloseResult:
    order_id: str
    closed_qty: Decimal
    settle_position: List[Dict[str, str]]


@dataclass
class PositionSummary:
    side: str
    size: float
    average_price: Optional[float] = None


class GMOBusinessError(Exception):
    def __init__(
        self,
        *,
        path: str,
        status: Any,
        messages: Sequence[Dict[str, Any]],
        body: str,
        http_status: int,
    ) -> None:
        self.path = path
        self.status = status
        self.messages = list(messages)
        self.body = body
        self.http_status = http_status
        summary = self._build_summary()
        super().__init__(summary)

    def _build_summary(self) -> str:
        if not self.messages:
            return f"GMO business error status={self.status}"
        primary = self.messages[0]
        code = primary.get("message_code")
        msg = primary.get("message_string")
        return f"GMO business error {code}: {msg}"

    @property
    def primary_code(self) -> Optional[str]:
        for item in self.messages:
            code = item.get("message_code")
            if code:
                return str(code)
        return None

    @property
    def primary_message(self) -> Optional[str]:
        for item in self.messages:
            msg = item.get("message_string")
            if msg:
                return str(msg)
        return None

    def has_code(self, code: str) -> bool:
        code_upper = code.upper()
        for item in self.messages:
            if str(item.get("message_code", "")).upper() == code_upper:
                return True
        return False


class GMOCoinClient:
    BASE_URL = "https://api.coin.z.com"

    def __init__(
        self,
        client: pybotters.Client,
        status_store: StatusStore,
        *,
        rest_timeout: float = 5.0,
        qty_step: Decimal = Decimal("0.01"),
    ) -> None:
        self._client = client
        self._status_store = status_store
        self._rest_timeout = rest_timeout
        self._qty_step = Decimal(str(qty_step))
        self._qty_decimals = max(-self._qty_step.as_tuple().exponent, 0)

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
                        extra = self._build_error_extra(
                            path=path, status=resp.status, body=text
                        )
                        extra["attempt"] = attempt
                        logger.warning("REST retry", extra=extra)
                        if attempt == len(backoff):
                            resp.raise_for_status()
                        await asyncio.sleep(delay)
                        continue
                    if resp.status >= 400:
                        logger.error(
                            "GMO REST HTTP error",
                            extra=self._build_error_extra(
                                path=path, status=resp.status, body=text
                            ),
                        )
                        resp.raise_for_status()
                    try:
                        data = await resp.json(content_type=None)
                    except Exception:
                        logger.error(
                            "GMO REST JSON decode error",
                            extra={"path": path, "status": resp.status, "body": text},
                        )
                        raise
                    self._ensure_gmo_success(
                        path=path, http_status=resp.status, payload=data, body=text
                    )
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

    def _build_error_extra(self, *, path: str, status: int, body: str) -> Dict[str, Any]:
        extra: Dict[str, Any] = {"path": path, "status": status, "body": body}
        parsed = self._parse_gmo_error(body)
        if parsed:
            extra.update(parsed)
        return extra

    @staticmethod
    def _parse_gmo_error(body: str) -> Optional[Dict[str, Any]]:
        try:
            data = json.loads(body)
        except Exception:
            return None
        messages = data.get("messages")
        return {
            "gmo_status": data.get("status"),
            "messages": messages,
        }

    def _ensure_gmo_success(
        self,
        *,
        path: str,
        http_status: int,
        payload: Dict[str, Any],
        body: str,
    ) -> None:
        status_value = payload.get("status")
        if status_value in (0, "0", "SUCCESS", "success"):
            return
        messages = payload.get("messages") or []
        logger.warning(
            "GMO business error",
            extra={
                "path": path,
                "http_status": http_status,
                "status": status_value,
                "messages": messages,
                "body": body,
            },
        )
        raise GMOBusinessError(
            path=path,
            status=status_value,
            messages=messages,
            body=body,
            http_status=http_status,
        )

    def _quantize(self, value: Decimal) -> Decimal:
        quantized = value.quantize(self._qty_step, rounding=ROUND_DOWN)
        return quantized

    def _format_size(self, value: Decimal) -> str:
        quantized = self._quantize(value)
        fmt = f"{{:.{self._qty_decimals}f}}"
        return fmt.format(quantized)

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
            size_value = entry.get("size") or entry.get("quantity") or 0
            size = float(size_value)
            price_raw = entry.get("averagePrice") or entry.get("average_price")
            avg_price = float(price_raw) if price_raw is not None else None
            if size > 0:
                await self._status_store.set_position(size, side)
                return PositionSummary(side=side, size=size, average_price=avg_price)
        await self._status_store.set_position(0.0, "FLAT")
        return PositionSummary(side="FLAT", size=0.0)

    async def fetch_settlement_size(
        self, symbol: str
    ) -> Tuple[Decimal, Decimal, List[Dict[str, str]]]:
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
            quantized_total = self._quantize(size_dec)
            if quantized_total <= 0:
                continue
            total_size += quantized_total
            quantized_settle = self._quantize(settle_dec)
            if quantized_settle <= 0:
                continue
            settable += quantized_settle
            settle_payload.append(
                {
                    "positionId": str(position_id),
                    "size": self._format_size(quantized_settle),
                }
            )
        return total_size, settable, settle_payload

    async def place_market_entry(
        self,
        *,
        symbol: str,
        side: str,
        size: Decimal,
        cash_margin_type: str | None = None,
    ) -> OrderResult:
        payload: Dict[str, Any] = {
            "symbol": symbol,
            "side": side,
            "executionType": "MARKET",
            "size": self._format_size(size),
        }
        if cash_margin_type:
            payload["cashMarginType"] = cash_margin_type
        data = await self._request("POST", "/private/v1/order", json_body=payload)
        order_data = data.get("data") or {}
        order_id = order_data.get("orderId") or order_data.get("order_id") or ""
        return OrderResult(order_id=order_id, status=str(data.get("status")), data=order_data)

    async def place_close_order(
        self,
        *,
        symbol: str,
        settle_position: List[Dict[str, str]],
        cash_margin_type: str | None = None,
    ) -> CloseResult:
        payload: Dict[str, Any] = {
            "symbol": symbol,
            "executionType": "MARKET",
            "settlePosition": settle_position,
        }
        if cash_margin_type:
            payload["cashMarginType"] = cash_margin_type
        data = await self._request("POST", "/private/v1/order", json_body=payload)
        order_data = data.get("data") or {}
        order_id = order_data.get("orderId") or order_data.get("order_id") or ""
        closed_qty = sum(Decimal(item["size"]) for item in settle_position)
        return CloseResult(
            order_id=order_id,
            closed_qty=closed_qty,
            settle_position=settle_position,
        )

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
