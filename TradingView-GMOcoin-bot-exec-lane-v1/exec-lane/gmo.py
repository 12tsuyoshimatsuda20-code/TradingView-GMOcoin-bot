from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence, Tuple

import httpx
import pybotters

REST_BASE_URL = "https://api.coin.z.com"
PRIVATE_PREFIX = "/private/v1"
WS_URL = "wss://api.coin.z.com/private/v1/ws"

JsonDict = Dict[str, Any]


@dataclass
class RestResult:
    data: JsonDict
    attempts: List[JsonDict]
    latency_ms: float


class GMOAPIError(Exception):
    def __init__(
        self,
        message: str,
        *,
        code: Optional[str] = None,
        status: Optional[int] = None,
        attempts: Optional[List[JsonDict]] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.attempts = attempts or []


class GMORetryableError(GMOAPIError):
    pass


def create_rest_client(timeout: float = 10.0) -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=REST_BASE_URL, timeout=timeout)


async def rest_request(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    api_key: str,
    api_secret: str,
    *,
    params: Optional[Dict[str, Any]] = None,
    json_body: Optional[Dict[str, Any]] = None,
    retry_backoff: Sequence[float] = (0.5, 1.0, 2.0),
    logger: Optional[logging.Logger] = None,
) -> RestResult:
    body_str = json.dumps(json_body, separators=(",", ":")) if json_body is not None else ""
    attempts: List[JsonDict] = []
    start = time.monotonic()
    for attempt, backoff in enumerate(retry_backoff, start=1):
        timestamp = str(int(time.time() * 1000))
        sign_target = f"{timestamp}{method}{path}{body_str}"
        signature = hmac.new(api_secret.encode(), sign_target.encode(), hashlib.sha256).hexdigest()
        headers = {
            "X-API-KEY": api_key,
            "X-SIGNATURE": signature,
            "X-TIMESTAMP": timestamp,
            "Content-Type": "application/json",
        }
        try:
            response = await client.request(
                method,
                path,
                params=params,
                content=body_str if json_body is not None else None,
                headers=headers,
            )
        except httpx.RequestError as exc:  # network/timeout
            attempts.append({"attempt": attempt, "error": str(exc), "type": "exception"})
            if attempt == len(retry_backoff):
                raise GMORetryableError("Request failed after retries", attempts=attempts) from exc
            if logger:
                logger.warning("REST request exception", extra={"attempt": attempt, "err": str(exc)})
            await asyncio.sleep(backoff)
            continue

        status = response.status_code
        content_type = response.headers.get("Content-Type", "")
        data: JsonDict
        if "application/json" in content_type:
            data = response.json()
        else:
            data = {"status": status, "body": response.text}

        if status in {429} or status >= 500:
            attempts.append(
                {
                    "attempt": attempt,
                    "status": status,
                    "type": "http_error",
                    "body": data,
                }
            )
            if attempt == len(retry_backoff):
                raise GMORetryableError(
                    f"HTTP {status} after retries", status=status, attempts=attempts
                )
            if logger:
                logger.warning("REST retry due to status", extra={"attempt": attempt, "status": status})
            await asyncio.sleep(backoff)
            continue

        if isinstance(data, dict) and data.get("status") not in (0, "0", None):
            code = None
            message = "Unknown error"
            messages = data.get("messages") or []
            if messages:
                first = messages[0]
                code = first.get("message_code")
                message = first.get("message_string", message)
            raise GMOAPIError(message, code=code, status=status, attempts=attempts)

        latency_ms = (time.monotonic() - start) * 1000.0
        return RestResult(data=data, attempts=attempts, latency_ms=latency_ms)

    raise GMORetryableError("Unhandled retry termination", attempts=attempts)


async def get_positions(
    client: httpx.AsyncClient,
    api_key: str,
    api_secret: str,
    symbol: str,
    *,
    logger: Optional[logging.Logger] = None,
) -> List[JsonDict]:
    result = await rest_request(
        client,
        "GET",
        f"{PRIVATE_PREFIX}/openPositions",
        api_key,
        api_secret,
        params={"symbol": symbol},
        logger=logger,
    )
    data = result.data.get("data") or []
    return data


async def market_entry(
    client: httpx.AsyncClient,
    api_key: str,
    api_secret: str,
    symbol: str,
    side: str,
    size: Decimal,
    *,
    logger: Optional[logging.Logger] = None,
) -> Tuple[JsonDict, RestResult]:
    payload = {
        "symbol": symbol,
        "side": side,
        "executionType": "MARKET",
        "size": format(size, "f"),
        "timeInForce": "FAS",
    }
    result = await rest_request(
        client,
        "POST",
        f"{PRIVATE_PREFIX}/order",
        api_key,
        api_secret,
        json_body=payload,
        logger=logger,
    )
    return result.data, result


async def market_close_all(
    client: httpx.AsyncClient,
    api_key: str,
    api_secret: str,
    symbol: str,
    *,
    logger: Optional[logging.Logger] = None,
) -> Tuple[JsonDict, RestResult]:
    positions = await get_positions(client, api_key, api_secret, symbol, logger=logger)
    if not positions:
        result = RestResult(data={"data": {"closed": []}}, attempts=[], latency_ms=0.0)
        return result.data, result

    settle_list: List[Dict[str, Any]] = []
    total_size = Decimal("0")
    close_side = None
    for pos in positions:
        pos_size = Decimal(pos.get("size", "0"))
        if pos_size <= 0:
            continue
        total_size += pos_size
        position_id = pos.get("positionId") or pos.get("position_id")
        settle_list.append({"positionId": position_id, "size": format(pos_size, "f")})
        side = pos.get("side")
        if side:
            close_side = "SELL" if side.upper() == "BUY" else "BUY"
    if total_size <= 0 or not settle_list:
        result = RestResult(data={"data": {"closed": []}}, attempts=[], latency_ms=0.0)
        return result.data, result

    payload = {
        "symbol": symbol,
        "side": close_side or "SELL",
        "executionType": "MARKET",
        "size": format(total_size, "f"),
        "settlePosition": settle_list,
        "timeInForce": "FAS",
    }

    try:
        result = await rest_request(
            client,
            "POST",
            f"{PRIVATE_PREFIX}/closeOrder",
            api_key,
            api_secret,
            json_body=payload,
            logger=logger,
        )
    except GMOAPIError as err:
        if err.code == "ERR-200":
            # settle quantity exceeds. try reducing sequentially
            trimmed_list: List[Dict[str, Any]] = []
            total = Decimal("0")
            for pos in settle_list:
                sz = Decimal(pos["size"])
                if total + sz > total_size:
                    sz = total_size - total
                total += sz
                if sz <= 0:
                    continue
                trimmed_list.append({"positionId": pos["positionId"], "size": format(sz, "f")})
            payload["settlePosition"] = trimmed_list
            payload["size"] = format(total, "f")
            result = await rest_request(
                client,
                "POST",
                f"{PRIVATE_PREFIX}/closeOrder",
                api_key,
                api_secret,
                json_body=payload,
                logger=logger,
            )
        else:
            raise

    return result.data, result


async def websocket_loop(
    api_key: str,
    api_secret: str,
    symbol: str,
    position_cb: Callable[[JsonDict], Awaitable[None]],
    execution_cb: Callable[[JsonDict], Awaitable[None]],
    status_cb: Callable[[bool], None],
    *,
    logger: Optional[logging.Logger] = None,
) -> None:
    backoff = 1.0
    while True:
        try:
            async with pybotters.Client(apis={"gmo": (api_key, api_secret)}) as client:
                async with client.ws_connect(WS_URL) as ws:
                    status_cb(True)
                    await ws.send_json(
                        {
                            "command": "subscribe",
                            "channel": "positionSummaryEvents",
                            "symbol": symbol,
                        }
                    )
                    await ws.send_json(
                        {
                            "command": "subscribe",
                            "channel": "executionEvents",
                            "symbol": symbol,
                        }
                    )
                    backoff = 1.0
                    async for msg in ws:
                        data = msg.json()
                        channel = data.get("channel")
                        if channel == "positionSummaryEvents":
                            await position_cb(data)
                        elif channel == "executionEvents":
                            await execution_cb(data)
        except Exception as exc:  # pragma: no cover - connection errors
            if logger:
                logger.error("WS loop error", extra={"err": str(exc)})
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)
            continue
        finally:
            status_cb(False)

