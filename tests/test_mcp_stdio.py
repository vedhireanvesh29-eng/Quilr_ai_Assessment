"""Exercise real SDK framing and legacy initialization through a subprocess."""

import asyncio
import json
import sys

import pytest


async def send(process, value):
    process.stdin.write((json.dumps(value) + "\n").encode())
    await process.stdin.drain()


async def receive(process):
    line = await asyncio.wait_for(process.stdout.readline(), timeout=10)
    assert line, "Server closed stdout"
    response = json.loads(line)
    assert response["jsonrpc"] == "2.0"
    return response


async def test_stdio_lifecycle_discovery_validation_and_tools():
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "ai_field_assessment.task1_mcp_server.server",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        await send(process, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "assessment-test", "version": "1.0"}
        }})
        initialized = await receive(process)
        assert "result" in initialized
        await send(process, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        await send(process, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        result = await receive(process)
        assert {tool["name"] for tool in result["result"]["tools"]} == {
            "get_customer_record", "trigger_refund"
        }
        for request_id, name, arguments in [
            (3, "get_customer_record", {"customer_id": "CUST-12345"}),
            (4, "trigger_refund", {"customer_id": "CUST-12345", "amount": 75.5,
                                   "reason": "Product arrived damaged"}),
        ]:
            await send(process, {"jsonrpc": "2.0", "id": request_id, "method": "tools/call",
                                 "params": {"name": name, "arguments": arguments}})
            result = await receive(process)
            assert "result" in result and not result["result"].get("isError", False)
        for name, arguments in [
            ("get_customer_record", {"customer_id": "invalid"}),
            ("get_customer_record", {"customer_id": "CUST-12345", "extra": 1}),
            ("unknown", {}),
            ("trigger_refund", {"customer_id": "CUST-12345", "amount": True, "reason": "long reason"}),
        ]:
            await send(process, {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                                 "params": {"name": name, "arguments": arguments}})
            result = await receive(process)
            assert result["error"]["code"] == -32602
        await send(process, {"jsonrpc": "2.0", "id": 6, "method": "nonexistent"})
        assert (await receive(process))["error"]["code"] == -32601
        process.stdin.write(b"{malformed\n")
        await process.stdin.drain()
        assert (await receive(process))["error"]["code"] == -32700
        # A malformed envelope is also refused, and the connection remains usable.
        await send(process, {"bad": "envelope"})
        assert (await receive(process))["error"]["code"] == -32600
        await send(process, {"jsonrpc": "2.0", "id": 9, "method": "tools/list"})
        assert "result" in await receive(process)
    finally:
        process.stdin.close()
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except TimeoutError:
            process.kill()
            await process.wait()
