import hashlib
import logging
import sqlite3
from contextlib import asynccontextmanager
from uuid import uuid4

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from ai_field_assessment.common.http import use_environment_proxy
from ai_field_assessment.config import Settings
from ai_field_assessment.task4_model_router.providers import HTTPProvider
from ai_field_assessment.task4_model_router.rate_limiter import RateLimiter, RateLimitExceeded
from ai_field_assessment.task4_model_router.router import ModelRouter, ProviderError


class CompletionInput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    prompt: str = Field(min_length=1, max_length=32000)
    max_output_tokens: int = Field(default=64, gt=0, le=50000)


def failure(code: str, status: int) -> JSONResponse:
    request_id = uuid4().hex
    logging.getLogger(__name__).warning(
        "gateway_failure code=%s request_id=%s status=%s", code, request_id, status
    )
    return JSONResponse(
        {
            "error": {
                "code": code,
                "message": "Unable to process this request",
                "request_id": request_id,
            }
        },
        status_code=status,
    )


def create_app(settings: Settings | None = None, transport=None) -> FastAPI:
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        limiter = RateLimiter(settings.database_path, settings.tokens_per_minute)
        await limiter.initialize()
        async with httpx.AsyncClient(
            timeout=settings.primary_timeout,
            transport=transport,
            trust_env=use_environment_proxy(
                settings.primary_provider_url, settings.backup_provider_url
            ),
        ) as client:
            app.state.router = ModelRouter(
                limiter,
                HTTPProvider(client, settings.primary_provider_url),
                HTTPProvider(client, settings.backup_provider_url),
                settings.primary_timeout,
            )
            yield

    app = FastAPI(title="Token quota and model fallback gateway", lifespan=lifespan)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError):
        return failure("INVALID_REQUEST", 422)

    @app.post("/completions")
    async def completions(body: CompletionInput, request: Request):
        # Demo API keys identify a tenant; production must authenticate these.
        api_key = request.headers.get("x-api-key", "")
        if not api_key or len(api_key) > 512:
            return failure("INVALID_REQUEST", 401)
        tenant = hashlib.sha256(api_key.encode()).hexdigest()
        try:
            result = await app.state.router.complete(tenant, body.prompt, body.max_output_tokens)
            return {"text": result.text, "model": result.model, "usage_tokens": result.usage_tokens}
        except RateLimitExceeded:
            return failure("RATE_LIMIT_EXCEEDED", 429)
        except TimeoutError:
            return failure("MODEL_UNAVAILABLE", 504)
        except ProviderError:
            return failure("PROVIDER_ERROR", 502)
        except sqlite3.Error:
            return failure("MODEL_UNAVAILABLE", 503)

    return app


app = create_app()
