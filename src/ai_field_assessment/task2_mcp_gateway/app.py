import json
import secrets
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from ai_field_assessment.common.jsonrpc import RPCError, error, validate_request
from ai_field_assessment.config import Settings


def create_app(settings: Settings | None = None, transport=None) -> FastAPI:
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with httpx.AsyncClient(timeout=10, transport=transport, follow_redirects=False) as client:
            app.state.client = client
            yield

    app = FastAPI(title="MCP security gateway", lifespan=lifespan)

    @app.post("/mcp")
    async def proxy(request: Request):
        header = request.headers.get("authorization", "").split()
        role = None
        if len(header) == 2 and header[0].lower() == "bearer":
            if secrets.compare_digest(header[1], settings.admin_token):
                role = "admin"
            elif secrets.compare_digest(header[1], settings.viewer_token):
                role = "viewer"
        if role is None:
            return JSONResponse(error(None, -32000, "Authentication required"), status_code=401)
        try:
            body = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            return JSONResponse(error(None, -32700, "Parse error"))
        try:
            body = validate_request(body)
        except RPCError as exc:
            return JSONResponse(error(None, exc.code, exc.message))
        request_id = body["id"]
        if body["method"] == "tools/call":
            params = body.get("params")
            if not isinstance(params, dict) or not isinstance(params.get("name"), str):
                return JSONResponse(error(request_id, -32602, "Invalid params"))
            if params["name"].startswith("admin_") and role != "admin":
                return JSONResponse(error(request_id, -32001, "Unauthorized Tool Call"))
        try:
            upstream = await request.app.state.client.post(settings.downstream_mcp_url, json=body)
        except httpx.TimeoutException:
            return JSONResponse(error(request_id, -32002, "Downstream timeout"), status_code=504)
        except httpx.RequestError:
            return JSONResponse(error(request_id, -32003, "Downstream unavailable"), status_code=502)
        # Only JSON is supported by this intentionally narrow mock transport.
        try:
            result = upstream.json()
            if not isinstance(result, dict) or result.get("jsonrpc") != "2.0":
                raise ValueError("Invalid envelope")
            if result.get("id") != request_id or (("error" in result) == ("result" in result)):
                raise ValueError("Invalid response")
        except (ValueError, UnicodeDecodeError):
            return JSONResponse(error(request_id, -32003, "Invalid downstream response"), status_code=502)
        return Response(upstream.content, status_code=upstream.status_code, media_type="application/json")

    return app


app = create_app()
