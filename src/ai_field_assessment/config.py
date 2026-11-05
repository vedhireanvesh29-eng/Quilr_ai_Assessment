"""Small immutable configuration object. Environment is read at application creation."""

import math
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    gateway_host: str = "127.0.0.1"
    gateway_port: int = 8000
    mcp_server_name: str = "Assessment Customers"
    downstream_mcp_url: str = "http://127.0.0.1:8001/mcp"
    admin_token: str = "dev-admin"
    viewer_token: str = "dev-viewer"
    llm_stream_url: str = "http://127.0.0.1:8002/generate"
    primary_provider_url: str = "http://127.0.0.1:8002/primary"
    backup_provider_url: str = "http://127.0.0.1:8002/backup"
    primary_timeout: float = 3.0
    database_path: str = "data/usage.db"
    tokens_per_minute: int = 50_000
    pii_buffer_size: int = 4096
    log_level: str = "INFO"

    def __post_init__(self) -> None:
        if not self.gateway_host or not 1024 <= self.gateway_port <= 65531:
            raise ValueError("Invalid gateway host or base port")
        if (
            not math.isfinite(self.primary_timeout)
            or self.primary_timeout <= 0
            or self.tokens_per_minute <= 0
            or self.pii_buffer_size < 32
        ):
            raise ValueError("Timeout, token limit and buffer size must be positive")
        if not self.admin_token or not self.viewer_token or self.admin_token == self.viewer_token:
            raise ValueError("Mock credentials must be nonempty and distinct")

    @classmethod
    def from_env(cls) -> "Settings":
        defaults = cls()
        values = {}
        for name in cls.__dataclass_fields__:
            default = getattr(defaults, name)
            raw = os.getenv(name.upper())
            values[name] = type(default)(raw) if raw is not None else default
        return cls(**values)
