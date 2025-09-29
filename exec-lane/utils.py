from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from typing import Any, Awaitable, Callable, Iterable, Optional

from loguru import logger


def utcnow() -> datetime:
    """Return timezone-aware UTC datetime."""
    return datetime.now(timezone.utc)


def ensure_aware(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def quantize_down(value: float, step: float) -> float:
    if step <= 0:
        raise ValueError("step must be positive")
    value_dec = Decimal(str(value))
    step_dec = Decimal(str(step))
    # quantize by flooring to the nearest step
    multiplier = (value_dec / step_dec).to_integral_value(rounding=ROUND_DOWN)
    return float(multiplier * step_dec)


def calculate_latency_ms(start: datetime, end: Optional[datetime] = None) -> int:
    end = end or utcnow()
    return int((end - start).total_seconds() * 1000)


def is_timestamp_fresh(ts: datetime, max_skew_seconds: int, now: Optional[datetime] = None) -> bool:
    now = now or utcnow()
    skew = abs((now - ensure_aware(ts)).total_seconds())
    return skew <= max_skew_seconds


async def async_retry(
    func: Callable[[], Awaitable[Any]],
    retries: int = 3,
    base_delay: float = 0.5,
    retry_exceptions: tuple[type[Exception], ...] = (Exception,),
    retry_statuses: Iterable[int] | None = None,
    fatal_statuses: Iterable[int] | None = None,
    on_retry: Optional[Callable[[int, Exception], Awaitable[None]]] = None,
) -> Any:
    retry_statuses = set(retry_statuses or [])
    fatal_statuses = set(fatal_statuses or [])

    attempt = 0
    delay = base_delay

    while True:
        try:
            response = await func()
            status = getattr(response, "status", None)
            if status is not None and status in fatal_statuses:
                return response
            if status is not None and retry_statuses and status in retry_statuses:
                raise RuntimeError(f"retryable status: {status}")
            return response
        except retry_exceptions as exc:  # type: ignore[misc]
            attempt += 1
            if attempt > retries:
                logger.error("retry exhausted", attempt=attempt, error=str(exc))
                raise
            logger.warning("retrying operation", attempt=attempt, delay=delay, error=str(exc))
            if on_retry:
                with contextlib.suppress(Exception):
                    await on_retry(attempt, exc)
            await asyncio.sleep(delay)
            delay *= 2


def mask_secret(secret: str, visible: int = 4) -> str:
    if not secret:
        return ""
    if len(secret) <= visible:
        return "*" * len(secret)
    return secret[:visible] + "*" * (len(secret) - visible)
