from __future__ import annotations
import asyncio
from typing import Optional, Literal
import httpx
from loguru import logger

Color = Literal["green", "gray", "red"]
COLOR_MAP = {"green": 0x2ECC71, "gray": 0x95A5A6, "red": 0xE74C3C}

async def _post_json(url: str, payload: dict, timeout_sec: float) -> None:
    async with httpx.AsyncClient(timeout=timeout_sec) as client:
        r = await client.post(url, json=payload)
        r.raise_for_status()

def _embed(title: str, desc: str, color: Color, fields: list[dict] | None = None) -> dict:
    out = {"title": title, "description": desc, "color": COLOR_MAP[color]}
    if fields: out["fields"] = fields
    return out

async def notify_discord(
    webhook_url: Optional[str],
    title: str,
    description: str,
    color: Color = "gray",
    fields: list[dict] | None = None,
    timeout_sec: float = 10.0,
) -> None:
    if not webhook_url:
        logger.debug("DISCORD_WEBHOOK empty; skip notify")
        return
    payload = {"embeds": [_embed(title, description, color, fields)]}
    try:
        await _post_json(webhook_url, payload, timeout_sec)
        logger.debug("Discord notified: {}", title)
    except httpx.HTTPStatusError as e:
        # 429/5xx などは WARNING で握る（アプリは落とさない）
        logger.warning("Discord HTTP error: status={} detail={}", e.response.status_code, e.response.text)
    except (httpx.ConnectError, httpx.ReadTimeout) as e:
        logger.warning("Discord network error: {}", repr(e))
    except Exception as e:
        logger.warning("Discord notify unexpected: {}", repr(e))
