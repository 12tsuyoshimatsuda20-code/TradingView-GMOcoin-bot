from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional

import httpx
from loguru import logger


class DiscordNotifier:
    def __init__(self, webhook_url: Optional[str], *, timeout: float = 10.0) -> None:
        self._webhook_url = webhook_url
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(timeout)) if webhook_url else None
        self._lock = asyncio.Lock()

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()

    async def send(
        self,
        *,
        title: str,
        description: str,
        color: int,
        fields: Optional[Dict[str, Any]] = None,
        footer: Optional[str] = None,
    ) -> None:
        if not self._webhook_url or not self._client:
            logger.debug("discord_webhook_disabled", title=title)
            return
        embed_fields = []
        if fields:
            for name, value in fields.items():
                embed_fields.append({"name": name, "value": str(value), "inline": True})
        payload = {
            "embeds": [
                {
                    "title": title,
                    "description": description,
                    "color": color,
                    "fields": embed_fields,
                    "footer": {"text": footer or "btcjpy-entry-close-bot"},
                }
            ]
        }
        async with self._lock:
            response = await self._client.post(self._webhook_url, json=payload)
            if response.status_code >= 400:
                logger.error(
                    "discord_notification_failed",
                    status_code=response.status_code,
                    body=response.text,
                )

    async def notify_success(self, title: str, description: str, fields: Optional[Dict[str, Any]] = None) -> None:
        await self.send(title=title, description=description, color=0x2ecc71, fields=fields)

    async def notify_warning(self, title: str, description: str, fields: Optional[Dict[str, Any]] = None) -> None:
        await self.send(title=title, description=description, color=0xf1c40f, fields=fields)

    async def notify_error(self, title: str, description: str, fields: Optional[Dict[str, Any]] = None) -> None:
        await self.send(title=title, description=description, color=0xe74c3c, fields=fields)

