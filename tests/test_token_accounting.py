"""Keep provider receipts distinct from the logical-request quota charge."""

import asyncio
import sqlite3

import pytest

from ai_field_assessment.task4_model_router.rate_limiter import RateLimiter, RateLimitExceeded
from ai_field_assessment.task4_model_router.router import Completion, ModelRouter, ProviderError


class Provider:
    def __init__(self, usage=None, text="ok", status=200):
        self.usage, self.text, self.status = usage, text, status
        self.calls = 0

    async def complete(self, prompt, max_output_tokens):
        self.calls += 1
        if self.status != 200:
            raise ProviderError(self.status)
        return Completion(self.text, "fixture", self.usage)


@pytest.mark.parametrize(
    "usage,text,charge",
    [
        (None, "ok", None),
        (0, "ok", 7),
        (2, "x" * 40, 45),
        (None, "x" * 40, 45),
        (80, "ok", 80),
    ],
)
async def test_estimates_receipts_and_charges_are_distinct(tmp_path, usage, text, charge):
    path = str(tmp_path / "quota.db")
    limiter = RateLimiter(path, limit=100, clock=lambda: 1000)
    await limiter.initialize()
    backup = Provider()
    await ModelRouter(limiter, Provider(usage, text), backup).complete("a", "hello", 10)
    with sqlite3.connect(path) as connection:
        row = connection.execute(
            "SELECT estimated_input,reserved_output,reserved,reported_usage,actual,"
            "reserved_at,completed_at FROM events"
        ).fetchone()
    assert row == (5, 10, 15, usage, charge, 1000, 1000)
    assert backup.calls == 0
    await limiter.reserve("a", 100 - (charge if charge is not None else 15))
    with pytest.raises(RateLimitExceeded):
        await limiter.reserve("a", 1)


@pytest.mark.parametrize("primary_status,backup_status", [(500, 200), (429, 200), (429, 500)])
async def test_unknown_attempts_keep_one_reservation(tmp_path, primary_status, backup_status):
    path = str(tmp_path / "quota.db")
    limiter = RateLimiter(path, limit=50, clock=lambda: 1000)
    await limiter.initialize()
    primary, backup = Provider(status=primary_status), Provider(0, status=backup_status)
    router = ModelRouter(limiter, primary, backup)
    if primary_status == 500 or backup_status == 500:
        with pytest.raises(ProviderError):
            await router.complete("a", "hello", 10)
    else:
        await router.complete("a", "hello", 10)
    with sqlite3.connect(path) as connection:
        rows = connection.execute(
            "SELECT reserved,actual,reported_usage,state FROM events"
        ).fetchall()
    expected = (
        (15, 15, 0, "complete")
        if (primary_status, backup_status) == (429, 200)
        else (15, None, None, "complete")
    )
    assert rows == [expected]
    assert (primary.calls, backup.calls) == (1, int(primary_status == 429))
    await limiter.reserve("a", 35)
    with pytest.raises(RateLimitExceeded):
        await limiter.reserve("a", 1)


async def test_cancelled_attempt_records_completion_without_receipt(tmp_path):
    started = asyncio.Event()
    cancelled = asyncio.Event()

    class BlockingProvider:
        async def complete(self, prompt, max_output_tokens):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    path = str(tmp_path / "quota.db")
    limiter = RateLimiter(path, limit=50, clock=lambda: 1000)
    await limiter.initialize()
    backup = Provider()
    task = asyncio.create_task(
        ModelRouter(limiter, BlockingProvider(), backup).complete("a", "hello", 45)
    )
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set() and backup.calls == 0
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT actual,reported_usage,state,reserved_at,completed_at FROM events"
        ).fetchone() == (None, None, "complete", 1000, 1000)
    with pytest.raises(RateLimitExceeded):
        await limiter.reserve("a", 1)
