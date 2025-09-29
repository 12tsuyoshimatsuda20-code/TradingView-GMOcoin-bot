from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Dict

import httpx

from .config import settings
from .logging import get_logger

logger = get_logger()


@dataclass(slots=True)
class DiscordMessage:
    title: str
    description: str
    color: int

    def to_payload(self) -> Dict[str, Any]:
        return {
            "embeds": [
                {
                    "title": self.title,
                    "description": self.description,
                    "color": self.color,
                }
            ]
        }


class DiscordNotifier:
    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client or httpx.AsyncClient(timeout=settings.discord_timeout)
        self._url = settings.notify_discord_webhook_url
        self._lock = asyncio.Lock()

    async def close(self) -> None:
        await self._client.aclose()

    async def send(self, message: DiscordMessage) -> None:
        if not self._url:
            return
        payload = message.to_payload()
        async with self._lock:
            try:
                response = await self._client.post(self._url, json=payload)
                if response.status_code >= 400:
                    logger.warning(
                        "discord notification failed",
                        extra={
                            "status_code": response.status_code,
                            "body": response.text,
                        },
                    )
            except httpx.HTTPError as exc:
                logger.warning(
                    "discord notification exception",
                    extra={"error": str(exc)},
                )


def build_success_message(*, title: str, fields: Dict[str, Any]) -> DiscordMessage:
    description = " | ".join(f"{key}={value}" for key, value in fields.items())
    return DiscordMessage(title=title, description=description, color=0x1ABC9C)


def build_error_message(*, title: str, fields: Dict[str, Any]) -> DiscordMessage:
    description = " | ".join(f"{key}={value}" for key, value in fields.items())
    return DiscordMessage(title=title, description=description, color=0xE74C3C)
