from __future__ import annotations

import asyncio
import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

@dataclass
class EventRecord:
    event_id: str
    ts: datetime
    mode: str


class RuntimeState:
    def __init__(
        self,
        *,
        db_path: Path,
        max_skew_seconds: int,
        qty_step: float,
        entry_policy: str,
    ) -> None:
        self._db_path = db_path
        self._max_skew_seconds = max_skew_seconds
        self._qty_step = qty_step
        self._entry_policy = entry_policy
        self._lock = asyncio.Lock()
        self._last_event_id: Optional[str] = None
        self._last_event_ts: Optional[datetime] = None
        self._position_side: Optional[str] = None
        self._position_qty: float = 0.0
        self._retry_stats: Dict[str, Dict[str, int]] = {}
        self._init_db()

    def _init_db(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self._db_path)
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS events (
                    event_id TEXT PRIMARY KEY,
                    ts TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT (datetime('now'))
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            conn.commit()
            row = conn.execute(
                "SELECT event_id, ts FROM events ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
            if row:
                self._last_event_id = row[0]
                try:
                    self._last_event_ts = datetime.fromisoformat(row[1])
                except ValueError:
                    self._last_event_ts = None
        finally:
            conn.close()

    async def _run_db(self, query: str, params: tuple = (), fetchone: bool = False) -> Any:
        def _execute() -> Any:
            conn = sqlite3.connect(self._db_path)
            try:
                cur = conn.execute(query, params)
                conn.commit()
                if fetchone:
                    return cur.fetchone()
                return cur.fetchall()
            finally:
                conn.close()

        return await asyncio.to_thread(_execute)

    async def record_event(self, event: EventRecord) -> None:
        await self._run_db(
            "INSERT OR REPLACE INTO events(event_id, ts, mode) VALUES (?, ?, ?)",
            (event.event_id, event.ts.isoformat(), event.mode),
        )
        async with self._lock:
            self._last_event_id = event.event_id
            self._last_event_ts = event.ts

    async def is_duplicate(self, event_id: str) -> bool:
        row = await self._run_db(
            "SELECT event_id FROM events WHERE event_id = ?",
            (event_id,),
            fetchone=True,
        )
        return row is not None

    def ensure_fresh(self, ts: datetime) -> None:
        now = datetime.now(timezone.utc)
        delta = abs((now - ts).total_seconds())
        if delta > self._max_skew_seconds:
            raise ValueError(f"timestamp_skew_exceeded: {delta:.2f}s > {self._max_skew_seconds}s")

    def floor_qty(self, size: float) -> float:
        if size <= 0:
            return 0.0
        steps = math.floor(size / self._qty_step)
        return round(steps * self._qty_step, 8)

    async def update_position(self, side: Optional[str], qty: float) -> None:
        async with self._lock:
            self._position_side = side
            self._position_qty = qty

    async def get_status(self) -> Dict[str, Any]:
        async with self._lock:
            status = {
                "position_side": self._position_side,
                "position_qty": self._position_qty,
                "last_event_id": self._last_event_id,
                "last_event_ts": self._last_event_ts.isoformat() if self._last_event_ts else None,
                "retry_stats": self._retry_stats,
            }
        return status

    def record_retry(self, key: str, success: bool) -> None:
        bucket = self._retry_stats.setdefault(key, {"success": 0, "failure": 0})
        bucket["success" if success else "failure"] += 1

    @property
    def entry_policy(self) -> str:
        return self._entry_policy

    @property
    def max_skew_seconds(self) -> int:
        return self._max_skew_seconds

    @property
    def qty_step(self) -> float:
        return self._qty_step

