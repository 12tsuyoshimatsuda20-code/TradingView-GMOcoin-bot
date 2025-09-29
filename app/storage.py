from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Optional

import aiosqlite
from loguru import logger


class IdempotencyStorage:
    def __init__(self, db_path: Path, ttl_seconds: int) -> None:
        self._db_path = db_path
        self._ttl_seconds = ttl_seconds
        self._conn: Optional[aiosqlite.Connection] = None
        self._lock = asyncio.Lock()
        self._memory: Dict[str, datetime] = {}
        self._use_memory = False

    async def initialize(self) -> None:
        if self._db_path:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._conn = await aiosqlite.connect(str(self._db_path))
            await self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS events (
                    event_id TEXT PRIMARY KEY,
                    created_at TIMESTAMP NOT NULL
                )
                """
            )
            await self._conn.commit()
            logger.info("Idempotency storage initialized with SQLite", path=str(self._db_path))
        except Exception as exc:  # pragma: no cover - fallback path
            logger.warning(
                "Falling back to in-memory idempotency storage", error=str(exc)
            )
            self._use_memory = True

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def _prune_sqlite(self) -> None:
        assert self._conn is not None
        cutoff = datetime.utcnow() - timedelta(seconds=self._ttl_seconds)
        await self._conn.execute(
            "DELETE FROM events WHERE created_at < ?",
            (cutoff.isoformat(),),
        )
        await self._conn.commit()

    async def register(self, event_id: str) -> bool:
        """Register event_id. Returns True if new, False if duplicate."""
        if self._use_memory or self._conn is None:
            now = datetime.utcnow()
            cutoff = now - timedelta(seconds=self._ttl_seconds)
            async with self._lock:
                expired = [k for k, v in self._memory.items() if v < cutoff]
                for key in expired:
                    self._memory.pop(key, None)
                if event_id in self._memory:
                    return False
                self._memory[event_id] = now
            return True

        async with self._lock:
            await self._prune_sqlite()
            try:
                await self._conn.execute(
                    "INSERT INTO events(event_id, created_at) VALUES (?, ?)",
                    (event_id, datetime.utcnow().isoformat()),
                )
                await self._conn.commit()
                return True
            except aiosqlite.IntegrityError:
                return False

