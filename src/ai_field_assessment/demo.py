"""Managed localhost subprocess cluster shared by the demo and live verifier."""

import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import TracebackType

SERVICES = {
    "mcp_backend": ("task2_mcp_gateway.mock_mcp_server", 1),
    "llm_backend": ("task3_stream_guardrail.mock_llm_provider", 2),
    "mcp_gateway": ("task2_mcp_gateway.app", 0),
    "stream_gateway": ("task3_stream_guardrail.app", 3),
    "model_router": ("task4_model_router.app", 4),
}


class DemoCluster:
    def __init__(
        self,
        base_port: int = 8000,
        startup_timeout: float = 20,
        database_path: str | None = None,
        host: str = "127.0.0.1",
    ) -> None:
        if base_port != 0 and not 1024 <= base_port <= 65531:
            raise ValueError("Base port must be zero or between 1024 and 65531")
        self.base_port, self.startup_timeout = base_port, startup_timeout
        self.database_path = database_path
        self.host = host
        self.connect_host = "127.0.0.1" if host == "0.0.0.0" else host
        self.processes: dict[str, subprocess.Popen] = {}
        self.ports: dict[str, int] = {}
        self.logs = []
        self.directory = None

    def url(self, name: str) -> str:
        return f"http://{self.connect_host}:{self.ports[name]}"

    def __enter__(self) -> "DemoCluster":
        try:
            probes = []
            try:
                for name, (_, offset) in SERVICES.items():
                    probe = socket.socket()
                    probes.append(probe)
                    probe.bind((self.host, self.base_port + offset if self.base_port else 0))
                    self.ports[name] = probe.getsockname()[1]
            finally:
                for probe in probes:
                    probe.close()
            self.directory = tempfile.TemporaryDirectory(prefix="quilr-demo-")
            environment = os.environ.copy()
            environment.update(
                {
                    "DOWNSTREAM_MCP_URL": self.url("mcp_backend") + "/mcp",
                    "LLM_STREAM_URL": self.url("llm_backend") + "/generate",
                    "PRIMARY_PROVIDER_URL": self.url("llm_backend") + "/primary",
                    "BACKUP_PROVIDER_URL": self.url("llm_backend") + "/backup",
                }
            )
            if self.database_path:
                environment["DATABASE_PATH"] = self.database_path
            for name, (module, _) in SERVICES.items():
                log = open(Path(self.directory.name) / f"{name}.log", "w", encoding="utf-8")
                self.logs.append(log)
                self.processes[name] = subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "uvicorn",
                        f"ai_field_assessment.{module}:app",
                        "--host",
                        self.host,
                        "--port",
                        str(self.ports[name]),
                    ],
                    env=environment,
                    stdout=log,
                    stderr=log,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
            deadline = time.monotonic() + self.startup_timeout
            while time.monotonic() < deadline:
                self.check_processes()
                ready = True
                for port in self.ports.values():
                    try:
                        with socket.create_connection((self.connect_host, port), timeout=0.1):
                            pass
                    except OSError:
                        ready = False
                        break
                if ready:
                    self.check_processes()
                    return self
                threading.Event().wait(0.05)  # Bounded readiness polling, not provider logic.
            raise TimeoutError("Demo services did not become ready before the startup deadline")
        except BaseException:
            self.close()
            raise

    def check_processes(self) -> None:
        for name, process in self.processes.items():
            if process.poll() is not None:
                raise RuntimeError(f"Demo service {name} exited with status {process.returncode}")

    def wait(self) -> None:
        while True:
            self.check_processes()
            threading.Event().wait(0.1)

    def close(self) -> None:
        for process in self.processes.values():
            if process.poll() is None:
                if os.name == "nt":
                    # Windows venv python.exe is a redirector: terminating only
                    # that parent leaves the actual Uvicorn child alive.
                    subprocess.run(
                        ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        creationflags=subprocess.CREATE_NO_WINDOW,
                        check=False,
                    )
                else:
                    process.terminate()
        for process in self.processes.values():
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        for log in self.logs:
            log.close()
        if self.directory is not None:
            self.directory.cleanup()

    def __exit__(
        self,
        kind: type[BaseException] | None,
        value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()
