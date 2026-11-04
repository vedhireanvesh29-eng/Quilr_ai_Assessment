import json
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from ai_field_assessment.config import Settings
from ai_field_assessment.task3_stream_guardrail.redactor import StreamingRedactor
from ai_field_assessment.task3_stream_guardrail.stream_parser import sse_data


class GenerationInput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    prompt: str = Field(min_length=1, max_length=32000)


def event(payload: dict) -> str:
    return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"


def create_app(settings: Settings | None = None, transport=None) -> FastAPI:
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with httpx.AsyncClient(timeout=30, transport=transport) as client:
            app.state.client = client
            yield

    app = FastAPI(title="Streaming PII guardrail", lifespan=lifespan)

    @app.post("/generate")
    async def generate(body: GenerationInput):
        async def stream():
            redactor = StreamingRedactor(settings.pii_buffer_size)
            completed = False
            try:
                async with app.state.client.stream(
                    "POST", settings.llm_stream_url, json=body.model_dump()
                ) as response:
                    response.raise_for_status()
                    async for data in sse_data(response.aiter_bytes()):
                        if data == "[DONE]":
                            completed = True
                            break
                        delta = json.loads(data)
                        if not isinstance(delta, dict) or not isinstance(delta.get("text"), str):
                            raise ValueError("Invalid delta")
                        safe = redactor.feed(delta["text"])
                        if safe:
                            yield event({"text": safe})
                if not completed:
                    raise ValueError("Missing completion marker")
                final = redactor.finish()
                if final:
                    yield event({"text": final})
                yield "data: [DONE]\n\n"
            except (httpx.HTTPError, ValueError, UnicodeError):
                # Never flush an uncertain candidate after an upstream failure.
                yield event({"error": {"code": "PROVIDER_ERROR", "message": "Stream unavailable"}})
            # CancelledError is deliberately not caught. The response context closes upstream.

        return StreamingResponse(stream(), media_type="text/event-stream", headers={
            "Cache-Control": "no-cache", "X-Accel-Buffering": "no"
        })

    return app


app = create_app()
