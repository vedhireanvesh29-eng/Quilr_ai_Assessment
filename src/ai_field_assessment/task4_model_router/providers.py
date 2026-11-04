import httpx

from ai_field_assessment.task4_model_router.router import Completion, ProviderError


class HTTPProvider:
    """Small adapter for the documented local JSON provider contract."""

    def __init__(self, client: httpx.AsyncClient, url: str) -> None:
        self.client, self.url = client, url

    async def complete(self, prompt: str, max_output_tokens: int) -> Completion:
        try:
            response = await self.client.post(self.url, json={
                "prompt": prompt, "max_output_tokens": max_output_tokens
            })
        except httpx.TimeoutException as exc:
            raise TimeoutError() from exc
        except httpx.RequestError as exc:
            raise ProviderError() from exc
        if response.status_code != 200:
            raise ProviderError(response.status_code)
        try:
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError("Invalid provider output")
            text, model, usage = data["text"], data["model"], data.get("usage_tokens")
            if not isinstance(text, str) or not isinstance(model, str):
                raise ValueError("Invalid provider output")
            if usage is not None and (type(usage) is not int or usage < 0):
                raise ValueError("Invalid provider usage")
            return Completion(text, model, usage)
        except (ValueError, KeyError) as exc:
            raise ProviderError() from exc
