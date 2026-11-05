"""Reservation-time sliding window with atomic SQLite admission and migration."""

import asyncio
import math
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

MAX_SQLITE_INTEGER = 2**63 - 1


class RateLimitExceeded(Exception):
    pass


@dataclass(frozen=True)
class Reservation:
    request_id: str
    tenant: str
    reserved_at: float
    reserved: int
    estimated_input: int | None = None
    reserved_output: int | None = None


class RateLimiter:
    def __init__(
        self,
        path: str,
        limit: int = 50_000,
        window: float = 60,
        clock: Callable[[], float] = time.time,
        stale_after: float = 120,
        retention: float = 86400,
    ) -> None:
        if (
            type(limit) is not int
            or not 0 < limit <= MAX_SQLITE_INTEGER
            or not all(
                math.isfinite(value) and value > 0 for value in (window, stale_after, retention)
            )
            or retention < stale_after + window
        ):
            raise ValueError("Invalid rate limiter configuration")
        self.path, self.limit, self.window = path, limit, window
        self.clock, self.stale_after, self.retention = clock, stale_after, retention

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def _now(self) -> float:
        value = self.clock()
        if not math.isfinite(value):
            raise ValueError("Clock must return a finite timestamp")
        return value

    @staticmethod
    def _create_table(connection: sqlite3.Connection) -> None:
        connection.execute("""
            CREATE TABLE events (
                request_id TEXT PRIMARY KEY,
                tenant TEXT NOT NULL,
                reserved_at REAL NOT NULL,
                completed_at REAL,
                reserved INTEGER NOT NULL CHECK (reserved > 0),
                actual INTEGER CHECK (actual >= 0),
                state TEXT NOT NULL CHECK (state IN ('pending','stale','complete')),
                estimated_input INTEGER CHECK (estimated_input >= 0),
                reserved_output INTEGER CHECK (reserved_output >= 0),
                reported_usage INTEGER CHECK (reported_usage >= 0)
            )
        """)

    def _initialize(self) -> None:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        connection = self._connect()
        try:
            # SQLite can return BUSY immediately for concurrent WAL conversion,
            # even with busy_timeout set. Retry only lock contention, bounded
            # independently of the persisted wall clock used for quota.
            deadline = time.monotonic() + 5
            while True:
                try:
                    connection.execute("PRAGMA journal_mode=WAL")
                    break
                except sqlite3.OperationalError as exc:
                    code = getattr(exc, "sqlite_errorcode", 0) & 0xFF
                    if code not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                        raise
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.01)
            connection.execute("BEGIN IMMEDIATE")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version > 2:
                raise ValueError("Database schema is newer than this application")
            columns = {row[1] for row in connection.execute("PRAGMA table_info(events)")}
            if not columns:
                self._create_table(connection)
            elif "timestamp" in columns and "reserved_at" not in columns:
                # Legacy timestamps were overwritten on completion. Their
                # original values cannot be recovered; retain the known time.
                connection.execute("ALTER TABLE events RENAME TO events_legacy_timestamp")
                self._create_table(connection)
                connection.execute("""
                    INSERT INTO events (
                        request_id, tenant, reserved_at, completed_at, reserved, actual, state
                    )
                    SELECT request_id, tenant, timestamp,
                           CASE WHEN state='complete' THEN timestamp ELSE NULL END,
                           reserved, actual, state
                    FROM events_legacy_timestamp
                """)
                connection.execute("DROP TABLE events_legacy_timestamp")
            elif not {
                "request_id",
                "tenant",
                "reserved_at",
                "completed_at",
                "reserved",
                "actual",
                "state",
                "estimated_input",
                "reserved_output",
                "reported_usage",
            }.issubset(columns):
                raise ValueError("Unsupported events schema")
            connection.execute(
                "CREATE INDEX IF NOT EXISTS events_tenant_reserved ON events(tenant,reserved_at)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS events_state_reserved ON events(state,reserved_at)"
            )
            connection.execute("PRAGMA user_version=2")
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    async def initialize(self) -> None:
        await asyncio.to_thread(self._initialize)

    def _reserve_event(
        self,
        tenant: str,
        tokens: int,
        estimated_input: int | None = None,
        reserved_output: int | None = None,
    ) -> Reservation:
        if type(tokens) is not int or not 0 < tokens <= MAX_SQLITE_INTEGER:
            raise ValueError("Tokens must be positive SQLite integers")
        if estimated_input is not None or reserved_output is not None:
            if (
                type(estimated_input) is not int
                or type(reserved_output) is not int
                or estimated_input < 0
                or reserved_output < 0
                or estimated_input + reserved_output != tokens
            ):
                raise ValueError("Input and output estimates must match the reservation")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            now = self._now()
            connection.execute(
                "UPDATE events SET state='stale' WHERE state='pending' AND reserved_at<=?",
                (now - self.stale_after,),
            )
            # Retention is operational history, separate from quota expiration.
            connection.execute(
                "DELETE FROM events WHERE state!='pending' AND reserved_at<=? "
                "AND COALESCE(completed_at,reserved_at)<=?",
                (now - self.retention, now - self.retention),
            )
            used = connection.execute(
                "SELECT COALESCE(SUM(COALESCE(actual,reserved)),0) FROM events "
                "WHERE tenant=? AND (state='pending' "
                "OR (state='complete' AND reserved_at>?) "
                "OR (state='stale' AND reserved_at>?))",
                (tenant, now - self.window, now - self.stale_after - self.window),
            ).fetchone()[0]
            if used + tokens > self.limit:
                connection.commit()
                raise RateLimitExceeded()
            reservation = Reservation(
                uuid4().hex, tenant, now, tokens, estimated_input, reserved_output
            )
            connection.execute(
                "INSERT INTO events (request_id,tenant,reserved_at,reserved,state,"
                "estimated_input,reserved_output) VALUES (?,?,?,?,'pending',?,?)",
                (reservation.request_id, tenant, now, tokens, estimated_input, reserved_output),
            )
            connection.commit()
            return reservation
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _reserve(self, tenant: str, tokens: int) -> str:
        return self._reserve_event(tenant, tokens).request_id

    async def reserve_context(
        self,
        tenant: str,
        tokens: int,
        *,
        estimated_input: int | None = None,
        reserved_output: int | None = None,
    ) -> Reservation:
        return await asyncio.to_thread(
            self._reserve_event, tenant, tokens, estimated_input, reserved_output
        )

    async def reserve(self, tenant: str, tokens: int) -> str:
        return (await self.reserve_context(tenant, tokens)).request_id

    def _reconcile(
        self,
        request_id: str,
        actual: int | None,
        reservation: Reservation | None = None,
        reported_usage: int | None = None,
    ) -> None:
        for value in (actual, reported_usage):
            if value is not None and (
                type(value) is not int or not 0 <= value <= MAX_SQLITE_INTEGER
            ):
                raise ValueError("Usage must be a nonnegative SQLite integer")
        if reservation is not None and reservation.request_id != request_id:
            raise ValueError("Reservation context does not match request")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            completed_at = self._now()
            changed = connection.execute(
                "UPDATE events SET actual=?, reported_usage=?, state='complete', completed_at=? "
                "WHERE request_id=? AND state IN ('pending','stale')",
                (actual, reported_usage, completed_at, request_id),
            ).rowcount
            exists = connection.execute(
                "SELECT 1 FROM events WHERE request_id=?", (request_id,)
            ).fetchone()
            if changed == 0 and exists is None:
                if reservation is None:
                    raise ValueError("Expired reservation requires its original context")
                connection.execute(
                    "INSERT INTO events (request_id,tenant,reserved_at,completed_at,reserved,"
                    "actual,state,estimated_input,reserved_output,reported_usage) "
                    "VALUES (?,?,?,?,?,?,'complete',?,?,?)",
                    (
                        request_id,
                        reservation.tenant,
                        reservation.reserved_at,
                        completed_at,
                        reservation.reserved,
                        actual,
                        reservation.estimated_input,
                        reservation.reserved_output,
                        reported_usage,
                    ),
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
        reservation: Reservation | None = None,
        reported_usage: int | None = None,
    ) -> None:
        await asyncio.to_thread(self._reconcile, request_id, actual, reservation, reported_usage)
