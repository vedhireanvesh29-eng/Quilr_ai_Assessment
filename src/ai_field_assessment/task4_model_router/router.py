import asyncio
from dataclasses import dataclass
from typing import Protocol

from ai_field_assessment.task4_model_router.rate_limiter import RateLimiter


class ProviderError(Exception):
    def __init__(self, status: int = 502) -> None:
        self.status = status
        super().__init__("Provider unavailable")


@dataclass(frozen=True)
class Completion:
    text: str
    model: str
    usage_tokens: int | None = None


class Provider(Protocol):
    async def complete(self, prompt: str, max_output_tokens: int) -> Completion: ...


class TokenCounter(Protocol):
    def count(self, prompt: str) -> int: ...


class UTF8TokenCounter:
    """Conservative byte count for byte/BPE providers; replace for other tokenizers."""

    def count(self, prompt: str) -> int:
        return len(prompt.encode("utf-8"))


class ModelRouter:
    def __init__(
        self,
        limiter: RateLimiter,
        primary: Provider,
        backup: Provider,
        timeout: float = 3,
        counter: TokenCounter | None = None,
    ) -> None:
        self.limiter, self.primary, self.backup = limiter, primary, backup
        self.timeout, self.counter = timeout, counter or UTF8TokenCounter()

    async def complete(self, tenant: str, prompt: str, max_output_tokens: int) -> Completion:
        reserved = self.counter.count(prompt) + max_output_tokens
        request_id = await self.limiter.reserve(tenant, reserved)
        actual = None
        fallback = False
        try:
            try:
                async with asyncio.timeout(self.timeout):
                    result = await self.primary.complete(prompt, max_output_tokens)
            except TimeoutError:
                fallback = True
            except ProviderError as exc:
                if exc.status != 429:
                    raise
                fallback = True
            if fallback:
                async with asyncio.timeout(self.timeout):
                    result = await self.backup.complete(prompt, max_output_tokens)
            # Failed attempts can consume unknown provider tokens. Retain the
            # entire single reservation on fallback, without charging twice.
            if not fallback and result.usage_tokens is not None:
                actual = result.usage_tokens
            return result
        finally:
            # Shield reconciliation from caller cancellation; quota is never refunded on errors.
            task = asyncio.create_task(self.limiter.reconcile(request_id, actual))
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                await task
                raise
