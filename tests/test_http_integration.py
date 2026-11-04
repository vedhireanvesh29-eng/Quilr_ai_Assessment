import asyncio
import json
from dataclasses import replace

import httpx
import pytest

from ai_field_assessment.config import Settings
from ai_field_assessment.task2_mcp_gateway.app import create_app as gateway
from ai_field_assessment.task2_mcp_gateway.mock_mcp_server import app as downstream
from ai_field_assessment.task3_stream_guardrail.app import create_app as guardrail
from ai_field_assessment.task3_stream_guardrail.mock_llm_provider import app as provider
from ai_field_assessment.task4_model_router.app import create_app as router_app


@pytest.mark.parametrize(
    "token,name,allowed",
    [
        ("dev-admin", "admin_reset_key", True),
        ("dev-viewer", "admin_reset_key", False),
        ("dev-viewer", "get_customer_record", True),
    ],
)
async def test_authorization(token, name, allowed):
    app = gateway(transport=httpx.ASGITransport(app=downstream))
    before = downstream.state.calls
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as c:
            response = await c.post(
                "/mcp",
                headers={"Authorization": f"Bearer {token}"},
                json={"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": name}},
            )
    if allowed:
        assert "result" in response.json()
        assert downstream.state.calls == before + 1
    else:
        assert response.json() == {
            "jsonrpc": "2.0",
            "id": 5,
            "error": {"code": -32001, "message": "Unauthorized Tool Call"},
        }
        assert downstream.state.calls == before


async def test_list_is_transparent_and_admin_visible():
    captured = []
    body = {"jsonrpc": "2.0", "id": "list", "method": "tools/list", "params": {"cursor": "x"}}

    def respond(request):
        captured.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": "list",
                "result": {"tools": [{"name": "admin_reset_key"}], "nextCursor": "y"},
            },
        )

    app = gateway(transport=httpx.MockTransport(respond))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as c:
            response = await c.post(
                "/mcp", headers={"Authorization": "Bearer dev-viewer"}, json=body
            )
    assert captured == [body]
    assert response.json()["result"]["tools"][0]["name"] == "admin_reset_key"


async def test_invalid_auth_json_and_params():
    app = gateway(transport=httpx.ASGITransport(app=downstream))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as c:
            for token in ["", "Bearer unknown", "Bearer dev-viewer extra"]:
                assert (
                    await c.post("/mcp", headers={"Authorization": token}, json={})
                ).status_code == 401
            header = {"Authorization": "Bearer dev-viewer"}
            response = await c.post("/mcp", headers=header, content="{")
            assert response.json()["error"]["code"] == -32700
            response = await c.post(
                "/mcp",
                headers=header,
                json={"jsonrpc": "2.0", "id": 8, "method": "tools/call", "params": {}},
            )
            assert response.json()["error"]["code"] == -32602


@pytest.mark.parametrize("exception", [httpx.ConnectError, httpx.ReadTimeout])
async def test_downstream_failure_sanitized(exception):
    def fail(request):
        raise exception("secret internal hostname")

    app = gateway(transport=httpx.MockTransport(fail))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as c:
            response = await c.post(
                "/mcp",
                headers={"Authorization": "Bearer dev-viewer"},
                json={"jsonrpc": "2.0", "id": 7, "method": "tools/list"},
            )
    assert response.status_code in {502, 504}
    assert "secret" not in response.text


async def test_streaming_redaction_http():
    app = guardrail(transport=httpx.ASGITransport(app=provider))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as c:
            response = await c.post(
                "/generate",
                json={"prompt": "Hi john@gmail.com SSN 123-45-6789 card 4111 1111 1111 1111. café"},
            )
    assert response.status_code == 200
    deltas = [
        json.loads(line[6:])["text"]
        for line in response.text.splitlines()
        if line.startswith("data: {")
    ]
    assert "".join(deltas) == "Hi [REDACTED] SSN [REDACTED] card [REDACTED]. café"
    assert "data: [DONE]" in response.text


async def test_malformed_sse_discards_pending_candidate():
    def respond(request):
        return httpx.Response(200, content='data: {"text":"john@gm"}\n\ndata: broken\n\n')

    app = guardrail(transport=httpx.MockTransport(respond))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as c:
            response = await c.post("/generate", json={"prompt": "test"})
    assert "PROVIDER_ERROR" in response.text
    assert "john" not in response.text
    assert "[DONE]" not in response.text


async def test_router_http_fallback_quota_and_errors(tmp_path):
    settings = replace(Settings(), database_path=str(tmp_path / "usage.db"), tokens_per_minute=80)
    app = router_app(settings, transport=httpx.ASGITransport(app=provider))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as c:
            response = await c.post(
                "/completions",
                headers={"X-API-Key": "tenant1"},
                json={"prompt": "[429] demo", "max_output_tokens": 40},
            )
            assert response.json()["model"] == "backup"
            limited = await c.post(
                "/completions",
                headers={"X-API-Key": "tenant1"},
                json={"prompt": "[429] demo", "max_output_tokens": 40},
            )
            assert limited.status_code == 429
            assert limited.json()["error"]["code"] == "RATE_LIMIT_EXCEEDED"
            invalid = await c.post("/completions", json={"prompt": "x", "max_output_tokens": True})
            assert invalid.json()["error"]["code"] == "INVALID_REQUEST"
            failure = await c.post(
                "/completions",
                headers={"X-API-Key": "tenant2"},
                json={"prompt": "[error] demo", "max_output_tokens": 10},
            )
            assert failure.status_code == 502
            assert "mock internal" not in failure.text


async def test_disconnect_closes_upstream():
    closed = asyncio.Event()

    class EndlessStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"text":"safe text "}\n\n'
            await asyncio.Event().wait()

        async def aclose(self):
            closed.set()

    app = guardrail(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=EndlessStream()))
    )
    async with app.router.lifespan_context(app):
        endpoint = next(route.endpoint for route in app.routes if route.path == "/generate")
        from ai_field_assessment.task3_stream_guardrail.app import GenerationInput

        response = await endpoint(GenerationInput(prompt="test"))
        generator = response.body_iterator
        assert "safe text" in await anext(generator)
        pending = asyncio.create_task(anext(generator))
        await asyncio.sleep(0)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        await asyncio.wait_for(closed.wait(), timeout=1)
