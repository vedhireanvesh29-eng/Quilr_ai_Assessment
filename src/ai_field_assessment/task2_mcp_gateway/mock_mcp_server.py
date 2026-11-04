from fastapi import FastAPI, Request

from ai_field_assessment.common.jsonrpc import error

app = FastAPI(title="Mock downstream MCP JSON-RPC endpoint")
app.state.calls = 0


@app.post("/mcp")
async def mcp(request: Request) -> dict:
    app.state.calls += 1
    body = await request.json()
    request_id = body["id"]
    if body["method"] == "tools/list":
        result = {
            "tools": [
                {"name": name, "inputSchema": {"type": "object"}}
                for name in ("get_customer_record", "admin_reset_key")
            ]
        }
    elif body["method"] == "tools/call":
        name = body["params"]["name"]
        if name not in {"get_customer_record", "admin_reset_key"}:
            return error(request_id, -32602, "Unknown tool")
        result = {
            "content": [{"type": "text", "text": f"Simulated {name} completed"}],
            "isError": False,
        }
    else:
        return error(request_id, -32601, "Method not found")
    return {"jsonrpc": "2.0", "id": request_id, "result": result}
