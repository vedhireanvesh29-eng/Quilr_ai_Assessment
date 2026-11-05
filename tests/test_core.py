"""Core invariants run with Python's standard library, even without web dependencies."""

import asyncio
import random
import tempfile
import unittest
from pathlib import Path

from ai_field_assessment.common.jsonrpc import RPCError, validate_request
from ai_field_assessment.task3_stream_guardrail.redactor import StreamingRedactor
from ai_field_assessment.task3_stream_guardrail.stream_parser import sse_data
from ai_field_assessment.task4_model_router.rate_limiter import RateLimiter, RateLimitExceeded
from ai_field_assessment.task4_model_router.router import Completion, ModelRouter, ProviderError


def redacted(text: str, sizes: list[int], maximum: int = 4096) -> tuple[str, int]:
    guard = StreamingRedactor(maximum)
    parts, offset = [], 0
    for size in sizes:
        parts.append(guard.feed(text[offset : offset + size]))
        offset += size
    parts.append(guard.feed(text[offset:]))
    parts.append(guard.finish())
    return "".join(parts), guard.high_water


class RedactionTests(unittest.TestCase):
    def test_every_split_and_random_layout(self):
        text = "Hello john@gmail.com SSN 123-45-6789 card 4111 1111 1111 1111. Thanks café!"
        expected = "Hello [REDACTED] SSN [REDACTED] card [REDACTED]. Thanks café!"
        for split in range(len(text) + 1):
            self.assertEqual(redacted(text, [split])[0], expected)
        rng = random.Random(7)
        for _ in range(100):
            self.assertEqual(redacted(text, [rng.randint(1, 8) for _ in text])[0], expected)

    def test_safe_text_preserved(self):
        text = "Hello 世界. Invoice 1234 costs 56 dollars.\nSee you soon!"
        self.assertEqual(redacted(text, [1] * len(text))[0], text)
        text = "Contact:<john@example.com>, please."
        self.assertEqual(redacted(text, [1] * len(text))[0], "Contact:<[REDACTED]>, please.")

    def test_long_candidate_fails_closed(self):
        for text in ["a" * 10000 + "@example.com end", "1 " * 10000 + "end"]:
            output, high_water = redacted(text, [1] * len(text), 32)
            self.assertEqual(output.count("[REDACTED]"), 1)
            self.assertNotIn("example.com", output)
            self.assertLessEqual(high_water, 32)

    def test_incomplete_email_and_luhn(self):
        self.assertEqual(redacted("contact john@", [3])[0], "contact [REDACTED]")
        self.assertEqual(redacted("4111-1111-1111-1111", [1] * 19)[0], "[REDACTED]")
        self.assertEqual(redacted("4111-1111-1111-1112", [1] * 19)[0], "4111-1111-1111-1112")

    def test_emits_before_finish(self):
        guard = StreamingRedactor()
        self.assertEqual(guard.feed("Hello world "), "Hello world ")
        self.assertEqual(guard.feed("john"), "")
        self.assertEqual(guard.feed("@example.com "), "[REDACTED] ")


class ParserTests(unittest.IsolatedAsyncioTestCase):
    async def test_utf8_crlf_comments_multiline(self):
        async def source():
            for byte in ": hi\r\ndata: café\r\ndata: world\r\n\r\n".encode():
                yield bytes([byte])

        self.assertEqual([data async for data in sse_data(source())], ["café\nworld"])

    async def test_truncated_and_oversized(self):
        for wire in [b"data: incomplete", b"data: " + b"a" * 100 + b"\n\n", b"data: \xff\n\n"]:

            async def source(wire=wire):
                yield wire

            with self.assertRaises((ValueError, UnicodeError)):
                _ = [data async for data in sse_data(source(), limit=32)]


class LimiterFixture:
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = str(Path(self.directory.name) / "usage.db")
        self.now = 1000.0
        self.limiter = RateLimiter(self.path, limit=50, clock=lambda: self.now)
        await self.limiter.initialize()

    async def asyncTearDown(self):
        self.directory.cleanup()


class LimiterTests(LimiterFixture, unittest.IsolatedAsyncioTestCase):
    async def test_atomic_independent_connections(self):
        others = [RateLimiter(self.path, limit=50, clock=lambda: self.now) for _ in range(3)]
        results = await asyncio.gather(
            *(other.reserve("tenant", 20) for other in others), return_exceptions=True
        )
        self.assertEqual(sum(isinstance(r, str) for r in results), 2)
        self.assertEqual(sum(isinstance(r, RateLimitExceeded) for r in results), 1)

    async def test_exact_limit_tenants_persistence_and_expiration(self):
        reservation = await self.limiter.reserve("a", 50)
        await self.limiter.reconcile(reservation, 50)
        with self.assertRaises(RateLimitExceeded):
            await RateLimiter(self.path, 50, clock=lambda: self.now).reserve("a", 1)
        await self.limiter.reserve("b", 50)
        self.now += 60
        await self.limiter.reserve("a", 50)

    async def test_reconciliation_and_failed_charge(self):
        reservation = await self.limiter.reserve("a", 50)
        await self.limiter.reconcile(reservation, 10)
        await self.limiter.reserve("a", 40)
        with self.assertRaises(RateLimitExceeded):
            await self.limiter.reserve("a", 1)

    async def test_active_and_stale_reservations(self):
        await self.limiter.reserve("a", 50)
        self.now += 61
        with self.assertRaises(RateLimitExceeded):
            await self.limiter.reserve("a", 1)
        self.now += 60
        with self.assertRaises(RateLimitExceeded):
            await self.limiter.reserve("a", 1)
        self.now += 60
        await self.limiter.reserve("a", 1)

    async def test_above_limit(self):
        with self.assertRaises(RateLimitExceeded):
            await self.limiter.reserve("a", 51)


class FakeProvider:
    def __init__(self, mode="ok"):
        self.mode, self.calls, self.cancelled = mode, 0, False
        self.started = asyncio.Event()

    async def complete(self, prompt, max_output_tokens):
        self.calls += 1
        self.started.set()
        if self.mode == "timeout":
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise
        if self.mode in {"429", "500"}:
            raise ProviderError(int(self.mode))
        return Completion("ok", self.mode, 2)


class RouterTests(LimiterFixture, unittest.IsolatedAsyncioTestCase):
    async def test_primary_success(self):
        primary, backup = FakeProvider(), FakeProvider("backup")
        router = ModelRouter(self.limiter, primary, backup, timeout=0.01)
        self.assertEqual((await router.complete("a", "hello", 5)).model, "ok")
        self.assertEqual(backup.calls, 0)

    async def test_fallback_429_and_timeout(self):
        for mode in ["429", "timeout"]:
            primary, backup = FakeProvider(mode), FakeProvider("backup")
            router = ModelRouter(self.limiter, primary, backup, timeout=0.01)
            result = await router.complete(mode, "hello", 5)
            self.assertEqual(result.model, "backup")
            self.assertEqual((primary.calls, backup.calls), (1, 1))
            if mode == "timeout":
                self.assertTrue(primary.cancelled)

    async def test_no_fallback_on_500(self):
        primary, backup = FakeProvider("500"), FakeProvider("backup")
        router = ModelRouter(self.limiter, primary, backup)
        with self.assertRaises(ProviderError):
            await router.complete("a", "hello", 5)
        self.assertEqual(backup.calls, 0)

    async def test_both_fail_and_quota_retained(self):
        router = ModelRouter(self.limiter, FakeProvider("429"), FakeProvider("500"))
        with self.assertRaises(ProviderError):
            await router.complete("a", "hello", 45)
        with self.assertRaises(RateLimitExceeded):
            await self.limiter.reserve("a", 1)

    async def test_cancellation_cleans_provider_and_accounts(self):
        primary = FakeProvider("timeout")
        router = ModelRouter(self.limiter, primary, FakeProvider())
        task = asyncio.create_task(router.complete("a", "hello", 45))
        await primary.started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(primary.cancelled)
        with self.assertRaises(RateLimitExceeded):
            await self.limiter.reserve("a", 1)


class ProtocolTests(unittest.TestCase):
    def test_invalid_envelopes(self):
        for body in [[], {}, {"jsonrpc": "2.0", "id": True, "method": "tools/list"}]:
            with self.assertRaises(RPCError):
                validate_request(body)


if __name__ == "__main__":
    unittest.main()
