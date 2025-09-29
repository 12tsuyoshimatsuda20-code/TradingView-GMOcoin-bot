from __future__ import annotations

import json
from typing import Any, Dict, Optional

import httpx
from loguru import logger


class Notifier:
    def __init__(self, webhook_url: Optional[str] = None, timeout: float = 10.0) -> None:
        self.webhook_url = webhook_url or ""
        self.timeout = timeout
        self._client: Optional[httpx.AsyncClient] = None

    async def start(self) -> None:
        if not self.webhook_url:
            logger.info("discord webhook disabled")
            return
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def send(self, level: str, title: str, message: str, *, extra: Optional[Dict[str, Any]] = None) -> None:
        if not self.webhook_url:
            return
        payload = {
            "username": "GMO Exec Bot",
            "embeds": [
                {
                    "title": f"[{level.upper()}] {title}",
                    "description": message,
                    "color": self._color(level),
                    "fields": [
                        {
                            "name": key,
                            "value": json.dumps(value, ensure_ascii=False)
                            if isinstance(value, (dict, list))
                            else str(value),
                            "inline": False,
                        }
                        for key, value in (extra or {}).items()
                    ],
                }
            ],
        }
        try:
            if not self._client:
                await self.start()
            if not self._client:
                return
            response = await self._client.post(self.webhook_url, json=payload)
            if response.status_code >= 400:
                logger.warning(
                    "discord webhook failed",
                    status_code=response.status_code,
                    body=response.text,
                )
        except Exception as exc:  # noqa: BLE001
            logger.error("discord webhook exception", error=str(exc))

    @staticmethod
    def _color(level: str) -> int:
        level = level.lower()
        if level == "info":
            return 0x2ecc71
        if level == "warning":
            return 0xf1c40f
        if level == "error":
            return 0xe74c3c
        return 0x95a5a6
