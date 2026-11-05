"""SQLite is the source of truth, including across processes.

Blocking database operations run in worker threads. Each transaction owns a
fresh connection; BEGIN IMMEDIATE serializes check-and-reserve across workers.
Active requests remain charged even after 60 seconds, until reconciled. Stale
reservations become conservative full charges at expiry rather than free usage.
"""

import asyncio
import sqlite3
import time
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from uuid import uuid4


class RateLimitExceeded(Exception):
    pass


class RateLimiter:
    def __init__(
        self,
        path: str,
        limit: int = 50_000,
        window: float = 60,
        clock: Callable[[], float] = time.time,
        stale_after: float = 120,
    ) -> None:
        if limit <= 0 or window <= 0 or stale_after <= 0:
            raise ValueError("Invalid rate limiter configuration")
        self.path, self.limit, self.window = path, limit, window
        self.clock, self.stale_after = clock, stale_after

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def _initialize(self) -> None:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS events (
                    request_id TEXT PRIMARY KEY,
                    tenant TEXT NOT NULL,
                    timestamp REAL NOT NULL,
                    reserved INTEGER NOT NULL CHECK (reserved > 0),
                    actual INTEGER,
                    state TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS tenant_window ON events(tenant, timestamp);
                CREATE INDEX IF NOT EXISTS event_expiration ON events(state, timestamp);
            """)

    async def initialize(self) -> None:
        await asyncio.to_thread(self._initialize)

    def _reserve(self, tenant: str, tokens: int) -> str:
        if tokens <= 0:
            raise ValueError("Tokens must be positive")
        now, request_id = self.clock(), uuid4().hex
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE events SET state='stale', timestamp=? "
                "WHERE state='pending' AND timestamp<=?",
                (now, now - self.stale_after),
            )
            connection.execute(
                "DELETE FROM events WHERE state!='pending' AND timestamp<=?", (now - self.window,)
            )
            used = connection.execute(
                "SELECT COALESCE(SUM(COALESCE(actual, reserved)),0) FROM events "
                "WHERE tenant=? AND (timestamp>? OR state='pending')",
                (tenant, now - self.window),
            ).fetchone()[0]
            if used + tokens > self.limit:
                # Commit housekeeping even when rejecting a request.
                connection.commit()
                raise RateLimitExceeded()
            connection.execute(
                "INSERT INTO events VALUES (?,?,?,?,NULL,'pending')",
                (request_id, tenant, now, tokens),
            )
            connection.commit()
            return request_id
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    async def reserve(self, tenant: str, tokens: int) -> str:
        # If cancelled, an in-flight worker may still reserve. Stale accounting
        # deliberately keeps that charge; it can never grant additional quota.
        return await asyncio.to_thread(self._reserve, tenant, tokens)

    def _reconcile(
        self,
        request_id: str,
        actual: int | None,
        tenant: str | None = None,
        reserved: int | None = None,
    ) -> None:
        if actual is not None and actual < 0:
            raise ValueError("Negative usage")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                "UPDATE events SET actual=?, state='complete', timestamp=? "
                "WHERE request_id=? AND state IN ('pending','stale')",
                (actual, self.clock(), request_id),
            ).rowcount
            if changed == 0:
                # A crashed/slow attempt may finish after stale cleanup. Restore
                # its charge with caller-retained attribution rather than lose it.
                if tenant is None or reserved is None:
                    raise ValueError("Unknown reservation requires recovery context")
                connection.execute(
                    "INSERT INTO events VALUES (?,?,?,?,?,'complete') "
                    "ON CONFLICT(request_id) DO NOTHING",
                    (request_id, tenant, self.clock(), reserved, actual),
                )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    async def reconcile(
        self,
        request_id: str,
        actual: int | None = None,
        *,
        tenant: str | None = None,
        reserved: int | None = None,
    ) -> None:
        await asyncio.to_thread(self._reconcile, request_id, actual, tenant, reserved)
