from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any, Dict, Optional

import pybotters
from loguru import logger


class GMOCoinAPIError(Exception):
    """Raised when GMO Coin API responds with an error."""

    def __init__(self, status_code: int, message: str, *, payload: Optional[str] = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload


class GMOCoinClient:
    BASE_URL = "https://api.coin.z.com"

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        *,
        client: Optional[pybotters.Client] = None,
        debug_signature: bool = False,
    ) -> None:
        self.api_key = api_key
        self.api_secret = api_secret
        self._client = client or pybotters.Client()
        self._owns_client = client is None
        self._debug_signature = debug_signature

    async def close(self) -> None:
        if self._owns_client:
            await self._client.close()

    def _create_signature(self, timestamp: str, method: str, path: str, body: str) -> str:
        message = f"{timestamp}{method.upper()}{path}{body}"
        signature = hmac.new(
            self.api_secret.encode(), message.encode(), hashlib.sha256
        ).hexdigest()
        if self._debug_signature:
            logger.debug(
                "signature_debug: ts=%s method=%s path=%s body=%s sign=%s",
                timestamp,
                method.upper(),
                path,
                body,
                signature,
            )
        return signature

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        if not path.startswith("/"):
            raise ValueError("path must start with '/' for GMO Coin API")

        timestamp = str(int(time.time() * 1000))
        body_text = json.dumps(json_body, separators=(",", ":")) if json_body else ""
        signature = self._create_signature(timestamp, method, path, body_text)

        headers = {
            "API-KEY": self.api_key,
            "API-TIMESTAMP": timestamp,
            "API-SIGN": signature,
            "Content-Type": "application/json",
        }

        url = f"{self.BASE_URL}{path}"
        session = self._client.session
        async with session.request(
            method.upper(), url, params=params, data=body_text or None, headers=headers
        ) as response:
            text = await response.text()
            if response.status >= 400:
                logger.error(
                    "GMO Coin API error: status=%s body=%s", response.status, text
                )
                raise GMOCoinAPIError(response.status, text, payload=text)

            if not text:
                return {}

            try:
                payload = json.loads(text)
            except json.JSONDecodeError as exc:
                logger.error("Failed to decode GMO Coin response: %s", text)
                raise GMOCoinAPIError(response.status, "Invalid JSON", payload=text) from exc

            return payload

    async def fetch_open_positions(self, symbol: str) -> Dict[str, Any]:
        return await self.request(
            "GET", "/private/v1/openPositions", params={"symbol": symbol}
        )

    async def place_market_order(self, symbol: str, side: str, size: float) -> Dict[str, Any]:
        payload = {
            "symbol": symbol,
            "side": side,
            "executionType": "MARKET",
            "size": f"{size:.8f}",
        }
        return await self.request("POST", "/private/v1/orders", json_body=payload)
