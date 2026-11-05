"""Exercise actual HTTP servers; mocks stay local and no paid keys are needed."""

import json
import os
import sys
from uuid import uuid4

import httpx
from mcp import Client
from mcp.client.stdio import StdioServerParameters

from ai_field_assessment.demo import DemoCluster


async def verify_mcp() -> dict:
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "ai_field_assessment.task1_mcp_server.server"],
        env=os.environ.copy(),
    )
    async with Client(params, read_timeout_seconds=10) as client:
        tools = await client.list_tools()
        assert {tool.name for tool in tools.tools} == {"get_customer_record", "trigger_refund"}
        customer = await client.call_tool("get_customer_record", {"customer_id": "CUST-12345"})
        refund = await client.call_tool(
            "trigger_refund",
            {"customer_id": "CUST-12345", "amount": 75.5, "reason": "Product arrived damaged"},
        )
        assert not customer.is_error and not refund.is_error
        assert "CUST-12345" in str(customer.content)
        assert "mock-" in str(refund.content) and "simulated" in str(refund.content)
    return {"mcp_startup": "passed", "customer_lookup": "passed", "mock_refund": "passed"}


def verify_http(cluster: DemoCluster) -> dict:
    with httpx.Client(timeout=10, trust_env=False) as client:
        endpoint = cluster.url("mcp_gateway") + "/mcp"
        header = {"Authorization": "Bearer dev-viewer"}
        discovery = client.post(
            endpoint, headers=header, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        ).json()
        assert any(tool["name"] == "admin_reset_key" for tool in discovery["result"]["tools"])
        blocked = client.post(
            endpoint,
            headers=header,
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "admin_reset_key"},
            },
        ).json()
        assert blocked["error"]["code"] == -32001 and blocked["id"] == 2
        permitted = client.post(
            endpoint,
            headers={"Authorization": "Bearer dev-admin"},
            json={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "admin_reset_key"},
            },
        ).json()
        assert "result" in permitted
        text = []
        done = False
        with client.stream(
            "POST",
            cluster.url("stream_gateway") + "/generate",
            json={
                "prompt": "Hello card=4111 1111 1111 1111 email:john@example.com SSN 123-45-6789."
            },
        ) as response:
            response.raise_for_status()
            for line in response.iter_lines():
                if line == "data: [DONE]":
                    done = True
                elif line.startswith("data: "):
                    text.append(json.loads(line[6:])["text"])
        assert done
        assert "".join(text) == "Hello card=[REDACTED] email:[REDACTED] SSN [REDACTED]."
        completions = cluster.url("model_router") + "/completions"
        tenant = {"X-API-Key": uuid4().hex}
        primary = client.post(
            completions, headers=tenant, json={"prompt": "hello", "max_output_tokens": 64}
        )
        assert primary.json()["model"] == "primary"
        fallback = client.post(
            completions, headers=tenant, json={"prompt": "[429] demo", "max_output_tokens": 64}
        )
        assert fallback.json()["model"] == "backup"
        limited = client.post(
            completions, headers=tenant, json={"prompt": "hello", "max_output_tokens": 50000}
        )
        assert limited.status_code == 429
    return {
        "discovery": "passed",
        "authorization": "passed",
        "streaming_redaction": "passed",
        "primary": "passed",
        "fallback": "passed",
        "quota": "passed",
    }
