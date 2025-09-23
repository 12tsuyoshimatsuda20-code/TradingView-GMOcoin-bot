from __future__ import annotations

import asyncio
import logging
from typing import Optional

import httpx

logger = logging.getLogger("notify")


async def send_discord_message(
    webhook_url: Optional[str],
    level: str,
    message: str,
    *,
    http_client: Optional[httpx.AsyncClient] = None,
) -> None:
    if not webhook_url:
        return

    payload = {"content": f"[{level}] {message}"}

    owned_client = False
    if http_client is None:
        http_client = httpx.AsyncClient(timeout=5.0)
        owned_client = True

    try:
        response = await http_client.post(webhook_url, json=payload)
        if response.status_code >= 400:
            logger.error(
                "Discord webhook failed",
                extra={"status": response.status_code, "body": response.text},
            )
    except Exception as exc:  # pragma: no cover - network errors
        logger.error("Discord webhook exception", extra={"err": str(exc)})
    finally:
        if owned_client:
            await http_client.aclose()


async def notify_entry_success(
    webhook_url: Optional[str],
    *,
    event_id: str,
    symbol: str,
    side: str,
    size: float,
    avg_px: Optional[float],
    latency_ms: float,
    http_client: Optional[httpx.AsyncClient] = None,
) -> None:
    avg_part = f" | avg_px={avg_px:.0f}" if avg_px is not None else ""
    message = (
        f"event_id={event_id} | symbol={symbol} | side={side} | size={size:.4f}"\
        f"{avg_part} | latency={int(latency_ms)}ms"
    )
    await send_discord_message(webhook_url, "INFO", message, http_client=http_client)


async def notify_close_success(
    webhook_url: Optional[str],
    *,
    event_id: str,
    symbol: str,
    closed_qty: float,
    avg_px: Optional[float],
    realized_pnl: Optional[float],
    latency_ms: float,
    http_client: Optional[httpx.AsyncClient] = None,
) -> None:
    avg_part = f" | avg_px={avg_px:.0f}" if avg_px is not None else ""
    pnl_part = (
        f" | realized_pnl={'+' if (realized_pnl or 0) >= 0 else ''}{realized_pnl:.0f} JPY"
        if realized_pnl is not None
        else ""
    )
    message = (
        f"event_id={event_id} | symbol={symbol} | closed_qty={closed_qty:.4f}"\
        f"{avg_part}{pnl_part} | latency={int(latency_ms)}ms"
    )
    await send_discord_message(webhook_url, "INFO", message, http_client=http_client)


async def notify_error(
    webhook_url: Optional[str],
    *,
    event_id: str,
    mode: str,
    symbol: str,
    attempt: str,
    code: Optional[str],
    msg: str,
    http_client: Optional[httpx.AsyncClient] = None,
) -> None:
    code_part = f" | code={code}" if code else ""
    message = (
        f"event_id={event_id} | mode={mode} | symbol={symbol} | attempt={attempt}"\
        f"{code_part} | msg={msg} | next=stop"
    )
    await send_discord_message(webhook_url, "ERROR", message, http_client=http_client)


async def notify_flat(
    webhook_url: Optional[str],
    *,
    event_id: str,
    symbol: str,
    http_client: Optional[httpx.AsyncClient] = None,
) -> None:
    message = f"event_id={event_id} | symbol={symbol} | status=already flat"
    await send_discord_message(webhook_url, "INFO", message, http_client=http_client)
