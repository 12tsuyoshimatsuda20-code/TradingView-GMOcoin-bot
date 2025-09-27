from __future__ import annotations

from typing import Any, Dict, Optional

import httpx
from loguru import logger

from settings import settings


LEVEL_EMOJIS = {
    "INFO": "ℹ️",
    "WARN": "⚠️",
    "ERROR": "❌",
}


async def post_discord(level: str, title: str, body: str, extra: Optional[Dict[str, Any]] = None) -> None:
    webhook_url = settings.discord_webhook
    level_name = level.upper()
    emoji = LEVEL_EMOJIS.get(level_name, "ℹ️")

    if not webhook_url:
        logger.log(level_name, "Discord webhook not configured", title=title, body=body, extra=extra)
        return

    payload = {
        "content": f"{emoji} **{title}**\n{body}",
    }

    if extra:
        extra_lines = "\n".join(f"{key}: {value}" for key, value in extra.items())
        payload["content"] += f"\n```\n{extra_lines}\n```"

    try:
        async with httpx.AsyncClient(timeout=settings.discord_timeout) as client:
            response = await client.post(webhook_url, json=payload)
            response.raise_for_status()
    except Exception as exc:  # pragma: no cover - network errors
        logger.warning("Failed to post Discord notification", error=str(exc), payload=payload)
