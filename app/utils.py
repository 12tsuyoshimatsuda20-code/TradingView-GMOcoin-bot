from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        raise ValueError("step must be positive")
    if value <= 0:
        return Decimal("0")
    normalized = (value / step).quantize(Decimal("1"), rounding=ROUND_DOWN)
    return (normalized * step).quantize(step, rounding=ROUND_DOWN)


def calculate_latency_ms(start: datetime, end: datetime | None = None) -> int:
    end = end or utcnow()
    return int((end - start).total_seconds() * 1000)
