"""Compose public SDK transports and runners, without patching SDK internals.

SDK stdio parsing yields ValidationError objects for malformed input. The default
runner logs them. This adapter emits the assessment's required JSON-RPC errors
and forwards every valid SDK-parsed message untouched to the SDK runner.
"""

import anyio
from mcp.server import MCPServer, Server
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.stdio import stdio_server
from mcp.shared.message import SessionMessage
from mcp.types import CallToolResult, JSONRPCMessage, ListToolsResult, TextContent
from pydantic import TypeAdapter, ValidationError

from ai_field_assessment.common.jsonrpc import error


async def run_stdio(registry: MCPServer, middleware) -> None:
    message_adapter = TypeAdapter(JSONRPCMessage)

    async def list_tools(ctx, params):
        return ListToolsResult(tools=await registry.list_tools())

    async def call_tool(ctx, params):
        try:
            return await registry.call_tool(params.name, params.arguments or {})
        except ToolError as exc:
            message = (
                "Customer not found"
                if str(exc) == "Customer not found"
                else "Customer operation unavailable"
            )
            return CallToolResult(
                content=[TextContent(type="text", text=message)],
                is_error=True,
            )

    server = Server(registry.name, on_list_tools=list_tools, on_call_tool=call_tool)
    server.middleware.append(middleware)
    async with stdio_server() as (raw_read, write):
        send, read = anyio.create_memory_object_stream(0)

        async def filter_messages():
            async with send:
                async for message in raw_read:
                    if isinstance(message, ValidationError):
                        parse_error = any(
                            item["type"] == "json_invalid" for item in message.errors()
                        )
                        code, description = (
                            (-32700, "Parse error") if parse_error else (-32600, "Invalid Request")
                        )
                        response = message_adapter.validate_python(
                            error(None, code, description), by_name=False
                        )
                        await write.send(SessionMessage(response))
                    elif isinstance(message, Exception):
                        response = message_adapter.validate_python(
                            error(None, -32600, "Invalid Request"), by_name=False
                        )
                        await write.send(SessionMessage(response))
                    else:
                        await send.send(message)

        async with anyio.create_task_group() as group:
            group.start_soon(filter_messages)
            await server.run(read, write, server.create_initialization_options())
            group.cancel_scope.cancel()
