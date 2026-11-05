"""Use enterprise proxy settings for remote endpoints, never for local mocks."""

from urllib.parse import urlsplit


def use_environment_proxy(*urls: str) -> bool:
    return not all(urlsplit(url).hostname in {"127.0.0.1", "localhost", "::1"} for url in urls)
