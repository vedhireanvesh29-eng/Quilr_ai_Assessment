"""Local deterministic SSE and completion providers. No paid API keys required."""

import asyncio
import json

from fastapi import FastAPI
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

app = FastAPI(title="Mock LLM providers")


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prompt: str
    max_output_tokens: int = Field(default=64, gt=0)


@app.post("/generate")
async def generate(body: Input):
    async def chunks():
        # Character deltas deliberately split every sensitive pattern.
        for char in body.prompt:
            yield "data: " + json.dumps({"text": char}, ensure_ascii=False) + "\n\n"
            await asyncio.sleep(0)
        yield "data: [DONE]\n\n"

    return StreamingResponse(chunks(), media_type="text/event-stream")


@app.post("/primary")
async def primary(body: Input):
    if body.prompt.startswith("[429]"):
        return JSONResponse({"error": "mock throttling"}, status_code=429)
    if body.prompt.startswith("[timeout]"):
        await asyncio.sleep(5)  # Explicit provider-delay fixture, not router logic.
    if body.prompt.startswith("[error]"):
        return JSONResponse({"error": "mock internal error"}, status_code=500)
    return completion(body, "primary")


def completion(body: Input, model: str) -> dict:
    text = (
        ("Simulated " + model + " response")
        .encode()[: body.max_output_tokens]
        .decode("utf-8", errors="ignore")
    )
    return {
        "text": text,
        "model": model,
        "usage_tokens": len(body.prompt.encode()) + len(text.encode()),
    }


@app.post("/backup")
async def backup(body: Input) -> dict:
    return completion(body, "backup")
