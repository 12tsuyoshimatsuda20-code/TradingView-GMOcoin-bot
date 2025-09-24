"""Discord webhook helper."""
from __future__ import annotations

import aiohttp
from loguru import logger
from typing import Optional


class Notifier:
    def __init__(self, webhook_url: Optional[str]) -> None:
        self.webhook_url = webhook_url

    async def _post(self, level: str, title: str, text: str) -> None:
        if not self.webhook_url:
            return
        payload = {
            "content": f"[{level}] {title} :: {text}"
        }
        timeout = aiohttp.ClientTimeout(total=5)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(self.webhook_url, json=payload) as response:
                    if response.status >= 400:
                        body = await response.text()
                        logger.warning(
                            "discord webhook failed status={} body={}",
                            response.status,
                            body,
                        )
        except Exception as exc:  # pragma: no cover - network errors are best-effort
            logger.warning("discord webhook exception err={}", exc)

    async def send_info(self, title: str, text: str) -> None:
        await self._post("INFO", title, text)

    async def send_error(self, title: str, text: str) -> None:
        await self._post("ERROR", title, text)
