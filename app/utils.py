from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_iso8601_z(value: str) -> datetime:
    normalized = value.replace("Z", "+00:00")
    dt = datetime.fromisoformat(normalized)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def quantize(value: float, step: float) -> float:
    d_value = Decimal(str(value))
    d_step = Decimal(str(step))
    quantized = (d_value // d_step) * d_step
    return float(quantized.quantize(d_step, rounding=ROUND_DOWN))
