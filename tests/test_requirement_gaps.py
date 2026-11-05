"""Regression tests tied to previously uncovered assessment requirements."""

import asyncio
import json
import subprocess
import sys
from dataclasses import replace

import httpx
import pytest

from ai_field_assessment.config import Settings
from ai_field_assessment.task2_mcp_gateway.app import create_app as gateway
from ai_field_assessment.task3_stream_guardrail.app import GenerationInput
from ai_field_assessment.task3_stream_guardrail.app import create_app as guardrail
from ai_field_assessment.task3_stream_guardrail.mock_llm_provider import app as mock_provider
from ai_field_assessment.task3_stream_guardrail.redactor import StreamingRedactor
from ai_field_assessment.task3_stream_guardrail.stream_parser import sse_data
from ai_field_assessment.task4_model_router.app import create_app as router_app
from ai_field_assessment.task4_model_router.rate_limiter import RateLimiter, RateLimitExceeded
from ai_field_assessment.task4_model_router.router import Completion, ModelRouter, ProviderError


@pytest.mark.parametrize("prefix", ["card=", '"', "(", "[", "payment:", "abc"])
@pytest.mark.parametrize("card", ["4111 1111 1111 1111", "3782 822463 10005"])
def test_task3_prefixed_cards_at_every_boundary(prefix, card):
    text = prefix + card + " done"
    for boundary in range(len(text) + 1):
        guard = StreamingRedactor()
        output = guard.feed(text[:boundary]) + guard.feed(text[boundary:]) + guard.finish()
        assert output == prefix + "[REDACTED] done"
        assert card not in output


@pytest.mark.parametrize("candidate", ["123-", "123-45-", "123-45-6"])
def test_task3_incomplete_ssn_at_eof(candidate):
    guard = StreamingRedactor()
    assert guard.feed(candidate) + guard.finish() == "[REDACTED]"


def test_task3_oversized_prefix_cannot_release_following_numeric_groups():
    guard = StreamingRedactor(32)
    text = "a" * 100 + "4111 1111 1111 1111 end"
    pieces = [guard.feed(char) for char in text]
    output = "".join(pieces) + guard.finish()
    assert "1111" not in output
    assert output.count("[REDACTED]") == 1
    assert guard.high_water <= 32


async def test_task3_bom_and_multibyte_boundaries():
    async def source():
        for value in b'\xef\xbb\xbfdata: {"text":"caf\xc3\xa9"}\r\n\r\n':
            yield bytes([value])

    assert [json.loads(value)["text"] async for value in sse_data(source())] == ["caf\u00e9"]


async def test_task3_mock_chunk_configuration_and_incomplete_stream():
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mock_provider), base_url="http://test"
    ) as client:
        response = await client.post(
            "/generate", json={"prompt": "abcdefgh", "chunk_size": 3, "truncate_after": 1}
        )
    assert json.loads(response.text.splitlines()[0][6:])["text"] == "abc"
    assert "[DONE]" not in response.text


async def test_task3_gateway_emits_before_upstream_finishes():
    release = asyncio.Event()
    closed = asyncio.Event()

    class GatedStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"text":"Safe prefix "}\n\n'
            await release.wait()
            yield b'data: {"text":"john@example.com"}\n\ndata: [DONE]\n\n'

        async def aclose(self):
            closed.set()

    app = guardrail(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=GatedStream()))
    )
    async with app.router.lifespan_context(app):
        endpoint = next(route.endpoint for route in app.routes if route.path == "/generate")
        response = await endpoint(GenerationInput(prompt="fixture"))
        iterator = response.body_iterator
        first = await asyncio.wait_for(anext(iterator), timeout=2)
        assert "Safe prefix " in first
        assert not release.is_set() and not closed.is_set()
        release.set()
        tail = "".join([part async for part in iterator])
        assert "john@example.com" not in tail and "[REDACTED]" in tail
        assert closed.is_set()


@pytest.mark.parametrize("maximum", [32, 4096])
def test_task3_large_safe_stream_preserves_text_with_bounded_state(maximum):
    guard = StreamingRedactor(maximum)
    text = "Ordinary safe prose. " * 10000
    output = "".join(guard.feed(text[offset : offset + 17]) for offset in range(0, len(text), 17))
    assert output + guard.finish() == text
    assert guard.high_water <= maximum


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
async def test_task2_nonfinite_json_is_parse_error(constant):
    calls = []
    app = gateway(transport=httpx.MockTransport(lambda request: calls.append(request)))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as c:
            response = await c.post(
                "/mcp",
                headers={"Authorization": "Bearer dev-viewer"},
                content='{"jsonrpc":"2.0","id":1,"method":"tools/list",'
                '"params":{"invalid":' + constant + "}}",
            )
    assert response.json()["error"]["code"] == -32700
    assert calls == []


async def test_task2_nonascii_token_returns_401():
    app = gateway(transport=httpx.MockTransport(lambda request: pytest.fail("backend reached")))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as c:
            result = await c.post("/mcp", headers={b"Authorization": b"Bearer \xff"}, json={})
    assert result.status_code == 401


async def test_task2_invalid_params_preserves_zero_id():
    app = gateway(transport=httpx.MockTransport(lambda request: pytest.fail("backend reached")))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as c:
            result = await c.post(
                "/mcp",
                headers={"Authorization": "Bearer dev-viewer"},
                json={
                    "jsonrpc": "2.0",
                    "id": 0,
                    "method": "tools/call",
                    "params": None,
                },
            )
    assert result.json()["id"] == 0
    assert result.json()["error"]["code"] == -32602


class UsageProvider:
    def __init__(self, usage, status=200):
        self.usage, self.status = usage, status

    async def complete(self, prompt, max_output_tokens):
        if self.status != 200:
            raise ProviderError(self.status)
        return Completion("ok", "fixture", self.usage)


async def test_task4_underreported_usage_cannot_refund_observed_tokens(tmp_path):
    limiter = RateLimiter(str(tmp_path / "usage.db"), limit=30)
    await limiter.initialize()
    router = ModelRouter(limiter, UsageProvider(0), UsageProvider(0))
    await router.complete("tenant", "hello", 10)
    await limiter.reserve("tenant", 23)  # 5 input bytes + 2 output bytes remain charged.
    with pytest.raises(RateLimitExceeded):
        await limiter.reserve("tenant", 1)


async def test_task4_fallback_overrun_usage_remains_charged(tmp_path):
    limiter = RateLimiter(str(tmp_path / "usage.db"), limit=30)
    await limiter.initialize()
    router = ModelRouter(limiter, UsageProvider(None, 429), UsageProvider(30))
    await router.complete("tenant", "hello", 10)
    with pytest.raises(RateLimitExceeded):
        await limiter.reserve("tenant", 1)


async def test_task4_late_completion_keeps_original_window(tmp_path):
    now = 1000.0
    limiter = RateLimiter(str(tmp_path / "usage.db"), limit=50, clock=lambda: now)
    await limiter.initialize()
    reservation = await limiter.reserve_context("tenant", 20)
    now += 121
    await limiter.reserve("other", 1)
    now += 61
    await limiter.reserve("other", 1)
    await limiter.reconcile(reservation.request_id, 30, reservation=reservation)
    with limiter._connect() as connection:
        row = connection.execute(
            "SELECT reserved_at,completed_at,actual FROM events WHERE request_id=?",
            (reservation.request_id,),
        ).fetchone()
    assert row == (1000, 1182, 30)
    await limiter.reserve("tenant", 50)


async def test_task4_duplicate_reconciliation_is_idempotent(tmp_path):
    limiter = RateLimiter(str(tmp_path / "usage.db"), limit=50)
    await limiter.initialize()
    request_id = await limiter.reserve("tenant", 30)
    await limiter.reconcile(request_id, 20)
    await limiter.reconcile(request_id, 1)
    await limiter.reserve("tenant", 30)
    with pytest.raises(RateLimitExceeded):
        await limiter.reserve("tenant", 1)


def test_task4_persistence_across_interpreter_restart(tmp_path):
    path = str(tmp_path / "usage.db")
    limiter = RateLimiter(path, limit=50)
    limiter._initialize()
    program = (
        "import sys; from ai_field_assessment.task4_model_router.rate_limiter "
        "import RateLimiter,RateLimitExceeded; "
        "limiter=RateLimiter(sys.argv[1],limit=50); "
        "limiter._reserve('tenant',int(sys.argv[2]))"
    )
    first = subprocess.run([sys.executable, "-c", program, path, "50"], capture_output=True)
    second = subprocess.run([sys.executable, "-c", program, path, "1"], capture_output=True)
    assert first.returncode == 0
    assert second.returncode != 0 and b"RateLimitExceeded" in second.stderr


@pytest.mark.parametrize("usage", [True, -1, "12", 2**64])
async def test_task4_bad_provider_usage_is_sanitized(tmp_path, usage):
    settings = replace(Settings(), database_path=str(tmp_path / "usage.db"))
    app = router_app(
        settings,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, json={"text": "ok", "model": "fixture", "usage_tokens": usage}
            )
        ),
    )
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as c:
            response = await c.post(
                "/completions",
                headers={"X-API-Key": "tenant"},
                json={"prompt": "hello", "max_output_tokens": 20},
            )
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "PROVIDER_ERROR"


async def test_task4_diagnostic_logs_are_correlated_and_sanitized(tmp_path, caplog):
    settings = replace(Settings(), database_path=str(tmp_path / "usage.db"))
    app = router_app(
        settings,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(500, json={"error": "private-upstream-detail"})
        ),
    )
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as c:
            response = await c.post(
                "/completions",
                headers={"X-API-Key": "private-tenant-key"},
                json={"prompt": "private-prompt", "max_output_tokens": 20},
            )
    assert response.json()["error"]["request_id"] in caplog.text
    for private in ["private-upstream-detail", "private-tenant-key", "private-prompt"]:
        assert private not in caplog.text + response.text


@pytest.mark.parametrize("timeout", [float("inf"), float("nan"), 0, -1])
def test_common_invalid_timeout_configuration(timeout):
    with pytest.raises(ValueError):
        Settings(primary_timeout=timeout)


def test_common_gateway_environment_configuration(monkeypatch):
    monkeypatch.setenv("GATEWAY_HOST", "localhost")
    monkeypatch.setenv("GATEWAY_PORT", "18000")
    settings = Settings.from_env()
    assert settings.gateway_host == "localhost" and settings.gateway_port == 18000


@pytest.mark.parametrize("port", [0, -1, 65532])
def test_common_invalid_gateway_port(port):
    with pytest.raises(ValueError):
        Settings(gateway_port=port)


@pytest.mark.parametrize("separator", ["  ", "\t", "\n", "\u00a0"])
def test_task3_card_whitespace_normalization_across_every_split(separator):
    text = "card=" + separator.join(["4111", "1111", "1111", "1111"]) + " done"
    for boundary in range(len(text) + 1):
        guard = StreamingRedactor()
        output = guard.feed(text[:boundary]) + guard.feed(text[boundary:]) + guard.finish()
        assert output == "card=[REDACTED] done"


async def test_task2_http_failure_cannot_forward_private_jsonrpc_details():
    app = gateway(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                500,
                json={
                    "jsonrpc": "2.0",
                    "id": 7,
                    "error": {"code": -32603, "message": "private database credentials"},
                },
            )
        )
    )
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/mcp",
                headers={"Authorization": "Bearer dev-viewer"},
                json={"jsonrpc": "2.0", "id": 7, "method": "tools/list"},
            )
    assert response.status_code == 502
    assert response.json()["id"] == 7
    assert "private" not in response.text
