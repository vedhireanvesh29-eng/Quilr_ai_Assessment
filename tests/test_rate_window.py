"""Reservation-time boundaries and transactional disk-schema migration."""

import asyncio
import sqlite3
import threading

import pytest

from ai_field_assessment.task4_model_router.rate_limiter import RateLimiter, RateLimitExceeded


def event(path, request_id):
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        return dict(
            connection.execute("SELECT * FROM events WHERE request_id=?", (request_id,)).fetchone()
        )


@pytest.mark.parametrize("delay", [0, 10, 59, 60])
async def test_completion_does_not_restart_window(tmp_path, delay):
    now = 1000.0
    path = str(tmp_path / "quota.db")
    limiter = RateLimiter(path, limit=50, clock=lambda: now)
    await limiter.initialize()
    reservation = await limiter.reserve_context("a", 50)
    now += delay
    await limiter.reconcile(reservation.request_id, 10)
    row = event(path, reservation.request_id)
    assert (row["reserved_at"], row["completed_at"], row["actual"]) == (1000, now, 10)
    if delay < 60:
        with pytest.raises(RateLimitExceeded):
            await limiter.reserve("a", 41)
    now = 1060
    await limiter.reserve("a", 50)


async def test_pending_spans_boundary_then_unknown_failure_expires(tmp_path):
    now = 1000.0
    limiter = RateLimiter(str(tmp_path / "quota.db"), limit=50, clock=lambda: now)
    await limiter.initialize()
    reservation = await limiter.reserve_context("a", 50)
    now = 1060
    with pytest.raises(RateLimitExceeded):
        await limiter.reserve("a", 1)
    await limiter.reconcile(reservation.request_id)
    await limiter.reserve("a", 50)


async def test_failed_request_uses_reservation_window(tmp_path):
    now = 1000.0
    limiter = RateLimiter(str(tmp_path / "quota.db"), limit=50, clock=lambda: now)
    await limiter.initialize()
    reservation = await limiter.reserve_context("a", 50)
    now = 1010
    await limiter.reconcile(reservation.request_id)
    now = 1059
    with pytest.raises(RateLimitExceeded):
        await limiter.reserve("a", 1)
    now = 1060
    await limiter.reserve("a", 50)


async def test_stale_grace_is_anchored_to_reservation(tmp_path):
    now = 1000.0
    path = str(tmp_path / "quota.db")
    limiter = RateLimiter(path, limit=50, clock=lambda: now)
    await limiter.initialize()
    reservation = await limiter.reserve_context("a", 50)
    for moment in (1120, 1179):
        now = moment
        with pytest.raises(RateLimitExceeded):
            await limiter.reserve("a", 1)
        row = event(path, reservation.request_id)
        assert row["state"] == "stale" and row["reserved_at"] == 1000
    now = 1180
    current_id = await limiter.reserve("a", 50)
    now = 1181
    await limiter.reconcile(reservation.request_id, 70, reservation=reservation)
    row = event(path, reservation.request_id)
    assert (row["reserved_at"], row["completed_at"], row["actual"]) == (1000, 1181, 70)
    # The new request remains charged; the late old completion adds no new window.
    with pytest.raises(RateLimitExceeded):
        await limiter.reserve("a", 1)
    now = 1240
    await limiter.reconcile(current_id, 50)
    await limiter.reserve("a", 50)


async def test_purged_history_recovers_original_context(tmp_path):
    now = 1000.0
    path = str(tmp_path / "quota.db")
    limiter = RateLimiter(path, limit=50, clock=lambda: now, retention=180)
    await limiter.initialize()
    reservation = await limiter.reserve_context("a", 50, estimated_input=5, reserved_output=45)
    now = 1180
    await limiter.reserve("b", 1)
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT 1 FROM events WHERE tenant='a'").fetchone() is None
    now = 1200
    await limiter.reconcile(reservation.request_id, 80, reservation=reservation, reported_usage=80)
    row = event(path, reservation.request_id)
    assert (row["reserved_at"], row["completed_at"]) == (1000, 1200)
    assert (
        row["actual"],
        row["reported_usage"],
        row["estimated_input"],
        row["reserved_output"],
    ) == (80, 80, 5, 45)
    await limiter.reserve("a", 50)


async def test_concurrent_20000_and_restart_preserve_tenants(tmp_path):
    now = 1000.0
    path = str(tmp_path / "quota.db")
    limiter = RateLimiter(path, clock=lambda: now)
    await limiter.initialize()
    peers = [RateLimiter(path, clock=lambda: now) for _ in range(3)]
    results = await asyncio.gather(
        *(peer.reserve("a", 20000) for peer in peers), return_exceptions=True
    )
    assert sum(isinstance(result, str) for result in results) == 2
    assert sum(isinstance(result, RateLimitExceeded) for result in results) == 1
    restarted = RateLimiter(path, clock=lambda: now)
    await restarted.initialize()
    await restarted.reserve("b", 50000)
    await restarted.reserve("a", 10000)
    now = 1060
    with pytest.raises(RateLimitExceeded):
        await restarted.reserve("a", 1)


def legacy_database(path, state="complete"):
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE events (request_id TEXT PRIMARY KEY,tenant TEXT NOT NULL,"
            "timestamp REAL NOT NULL,reserved INTEGER NOT NULL,actual INTEGER,state TEXT NOT NULL)"
        )
        connection.execute("CREATE INDEX events_tenant_time ON events(tenant,timestamp)")
        connection.executemany(
            "INSERT INTO events VALUES (?,?,?,?,?,?)",
            [
                ("done", "a", 990, 30, 20, state),
                ("working", "b", 900, 50, None, "pending"),
                ("old", "c", 850, 10, None, "stale"),
            ],
        )


async def test_disk_migration_preserves_quota_and_is_repeatable(tmp_path):
    path = str(tmp_path / "legacy.db")
    legacy_database(path)
    limiter = RateLimiter(path, limit=50, clock=lambda: 1000)
    await asyncio.gather(limiter.initialize(), RateLimiter(path).initialize())
    row = event(path, "done")
    assert (
        row["reserved_at"],
        row["completed_at"],
        row["reserved"],
        row["actual"],
        row["state"],
    ) == (990, 990, 30, 20, "complete")
    assert row["estimated_input"] is None and row["reported_usage"] is None
    with pytest.raises(RateLimitExceeded):
        await limiter.reserve("a", 31)
    with pytest.raises(RateLimitExceeded):
        await limiter.reserve("b", 1)
    assert event(path, "working")["reserved_at"] == 900
    assert event(path, "old")["reserved_at"] == 850
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
        assert connection.execute("SELECT count(*) FROM events").fetchone()[0] == 3


async def test_invalid_legacy_data_rolls_back_whole_migration(tmp_path):
    path = str(tmp_path / "legacy.db")
    legacy_database(path, "unexpected")
    with pytest.raises(sqlite3.IntegrityError):
        await RateLimiter(path).initialize()
    with sqlite3.connect(path) as connection:
        assert "timestamp" in {row[1] for row in connection.execute("PRAGMA table_info(events)")}
        assert connection.execute("SELECT count(*) FROM events").fetchone()[0] == 3
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 0


async def test_future_schema_is_not_downgraded(tmp_path):
    path = str(tmp_path / "future.db")
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA user_version=3")
    with pytest.raises(ValueError, match="newer"):
        await RateLimiter(path).initialize()
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 3


async def test_wal_initialization_retries_real_lock_contention(tmp_path):
    path = str(tmp_path / "locked.db")
    legacy_database(path)
    busy_seen = threading.Event()

    class ObservedConnection(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            try:
                return super().execute(sql, parameters)
            except sqlite3.OperationalError:
                if sql == "PRAGMA journal_mode=WAL":
                    busy_seen.set()
                raise

    class ImmediateBusyLimiter(RateLimiter):
        def _connect(self):
            return sqlite3.connect(
                self.path, timeout=0, isolation_level=None, factory=ObservedConnection
            )

    blocker = sqlite3.connect(path, isolation_level=None)
    blocker.execute("BEGIN EXCLUSIVE")
    task = asyncio.create_task(ImmediateBusyLimiter(path).initialize())
    try:
        assert await asyncio.to_thread(busy_seen.wait, 3), "WAL conversion never encountered lock"
    finally:
        blocker.rollback()
        blocker.close()
    await asyncio.wait_for(task, 6)
    assert event(path, "done")["reserved_at"] == 990
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
