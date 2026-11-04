"""SDK owns parsing, discovery, lifecycle and stdio. No stdout logging."""

import logging
import sys

import anyio
from mcp import MCPError
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import ValidationError

from ai_field_assessment.config import Settings
from ai_field_assessment.task1_mcp_server.customer_service import (
    CustomerNotFound,
    get_customer,
    refund,
)
from ai_field_assessment.task1_mcp_server.schemas import (
    Amount,
    CustomerID,
    CustomerInput,
    Reason,
    RefundInput,
)


async def validate_tool_call(ctx, call_next):
    """Public v2 middleware refuses malformed tool calls with top-level -32602."""
    if ctx.method == "tools/call":
        params = ctx.params
        if not isinstance(params, dict):
            raise MCPError(code=-32602, message="Invalid params")
        schema = {"get_customer_record": CustomerInput, "trigger_refund": RefundInput}.get(
            params.get("name")
        )
        if schema is None:
            raise MCPError(code=-32602, message="Unknown tool")
        try:
            schema.model_validate(params.get("arguments", {}))
        except ValidationError as exc:
            raise MCPError(code=-32602, message="Invalid tool arguments") from exc
    return await call_next(ctx)


def create_server() -> MCPServer:
    server = MCPServer(Settings.from_env().mcp_server_name, middleware=[validate_tool_call])

    @server.tool()
    def get_customer_record(customer_id: CustomerID) -> dict:
        """Look up a simulated customer with an exact CUST-XXXXX identifier."""
        try:
            return get_customer(customer_id)
        except CustomerNotFound as exc:
            raise ToolError("Customer not found") from exc

    @server.tool()
    def trigger_refund(customer_id: CustomerID, amount: Amount, reason: Reason) -> dict:
        """Create a simulated refund; no real payment is made."""
        try:
            return refund(customer_id, amount, reason)
        except CustomerNotFound as exc:
            raise ToolError("Customer not found") from exc

    return server


def main() -> None:
    from ai_field_assessment.task1_mcp_server.transport import run_stdio

    logging.basicConfig(stream=sys.stderr, level=Settings.from_env().log_level)
    anyio.run(run_stdio, create_server(), validate_tool_call)


if __name__ == "__main__":
    main()
