"""Measure the actual services and generate reproducible README figures.

All providers are controlled mocks. No vendor performance is inferred.
Run after pytest writes results.xml:
uv run --with matplotlib==3.10.7 python scripts/benchmark_report.py
"""

import asyncio
import base64
import json
import os
import platform
import statistics
import tempfile
import time
import tracemalloc
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import httpx
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from mcp import Client

from ai_field_assessment.task1_mcp_server.server import create_server
from ai_field_assessment.task2_mcp_gateway.app import create_app
from ai_field_assessment.task3_stream_guardrail.redactor import StreamingRedactor
from ai_field_assessment.task4_model_router.rate_limiter import RateLimiter, RateLimitExceeded
from ai_field_assessment.task4_model_router.router import Completion, ModelRouter, ProviderError

OUTPUT = Path("docs")
IMAGES = OUTPUT / "images"


def distribution(samples: list[float]) -> dict:
    ordered = sorted(samples)
    index = min(len(ordered) - 1, int(0.95 * len(ordered)))
    return {
        "n": len(samples),
        "median_ms": round(statistics.median(samples), 4),
        "p95_ms": round(ordered[index], 4),
        "samples_ms": [round(value, 4) for value in samples],
    }


def test_counts() -> dict:
    groups = {
        key: {"passed": 0, "failed": 0, "skipped": 0, "total": 0}
        for key in ["task1", "task2", "task3", "task4", "common"]
    }
    for case in ET.parse("results.xml").getroot().iter("testcase"):
        classname, name = case.get("classname", ""), case.get("name", "")
        if "test_requirement_gaps" in classname or "test_live_demo" in classname:
            group = name.split("_")[1]
            if group not in groups:
                raise ValueError(f"Unclassified regression: {name}")
        elif "test_schemas" in classname or "test_mcp_stdio" in classname:
            group = "task1"
        elif "test_http_integration" in classname:
            if name.startswith(("test_streaming", "test_malformed_sse", "test_disconnect")):
                group = "task3"
            elif name.startswith("test_router"):
                group = "task4"
            else:
                group = "task2"
        elif "RedactionTests" in classname or "ParserTests" in classname:
            group = "task3"
        elif "LimiterTests" in classname or "RouterTests" in classname:
            group = "task4"
        elif "ProtocolTests" in classname:
            group = "common"
        else:
            raise ValueError(f"Unclassified test: {classname}.{name}")
        state = "passed"
        if case.find("failure") is not None or case.find("error") is not None:
            state = "failed"
        elif case.find("skipped") is not None:
            state = "skipped"
        groups[group][state] += 1
        groups[group]["total"] += 1
    groups["all"] = {
        key: sum(item[key] for item in groups.values())
        for key in ["passed", "failed", "skipped", "total"]
    }
    return groups


async def endpoint_benchmarks() -> dict:
    mcp_times = []
    async with Client(create_server()) as client:
        await client.call_tool("get_customer_record", {"customer_id": "CUST-12345"})
        for _ in range(30):
            started = time.perf_counter()
            result = await client.call_tool("get_customer_record", {"customer_id": "CUST-12345"})
            assert not result.is_error
            mcp_times.append((time.perf_counter() - started) * 1000)

    calls = 0

    def downstream(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        body = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": body["id"],
                "result": {"content": [{"type": "text", "text": "mock"}]},
            },
        )

    app = create_app(transport=httpx.MockTransport(downstream))
    measurements = {}
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://benchmark"
        ) as client:
            for name, tool in [
                ("proxy_allowed", "get_customer_record"),
                ("proxy_blocked", "admin_reset_key"),
            ]:
                samples = []
                before = calls
                for index in range(30):
                    started = time.perf_counter()
                    response = await client.post(
                        "/mcp",
                        headers={"Authorization": "Bearer dev-viewer"},
                        json={
                            "jsonrpc": "2.0",
                            "id": index,
                            "method": "tools/call",
                            "params": {"name": tool, "arguments": {}},
                        },
                    )
                    samples.append((time.perf_counter() - started) * 1000)
                    if name == "proxy_blocked":
                        assert response.json()["error"]["code"] == -32001
                    else:
                        assert "result" in response.json()
                measurements[name] = distribution(samples)
                measurements[name]["backend_calls"] = calls - before
    assert measurements["proxy_blocked"]["backend_calls"] == 0
    return {"mcp_call": distribution(mcp_times), **measurements}


def streaming_benchmarks() -> list[dict]:
    text = "Safe text then john@example.com and 4111 1111 1111 1111. " * 2000
    rows = []
    for size in [1, 16, 64, 256, 1024]:
        totals, firsts = [], []
        for _ in range(7):
            guard = StreamingRedactor()
            first = None
            started = time.perf_counter()
            for offset in range(0, len(text), size):
                safe = guard.feed(text[offset : offset + size])
                if safe and first is None:
                    first = time.perf_counter() - started
            guard.finish()
            totals.append((time.perf_counter() - started) * 1000)
            firsts.append((first or 0) * 1000)
        guard = StreamingRedactor()
        tracemalloc.start()
        for offset in range(0, len(text), size):
            guard.feed(text[offset : offset + size])
        guard.finish()
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        rows.append(
            {
                "chunk_chars": size,
                "input_chars": len(text),
                "total": distribution(totals),
                "first_output": distribution(firsts),
                "peak_traced_bytes": peak,
                "max_pending_chars": guard.high_water,
                "configured_pending_limit_chars": guard.max_buffer,
            }
        )
    return rows


async def quota_benchmarks(directory: str) -> dict:
    now = 0.0
    path = str(Path(directory) / "quota.db")
    limiter = RateLimiter(path, clock=lambda: now)
    await limiter.initialize()
    rows = []
    events: list[tuple[float, int]] = []
    for timestamp, requests in [
        (0, [20000] * 3),
        (15, [5000] * 2),
        (30, [1]),
        (59, [1]),
        (60, [20000] * 3),
        (75, [10000, 1]),
    ]:
        now = timestamp

        async def reserve(tokens: int, at: float = now) -> tuple[bool, int]:
            connection_owner = RateLimiter(path, clock=lambda: at)
            try:
                reservation = await connection_owner.reserve("measured-tenant", tokens)
            except RateLimitExceeded:
                return False, tokens
            await connection_owner.reconcile(reservation, tokens)
            return True, tokens

        started = time.perf_counter()
        outcomes = await asyncio.gather(*(reserve(tokens) for tokens in requests))
        duration = (time.perf_counter() - started) * 1000
        for accepted, tokens in outcomes:
            if accepted:
                events.append((now, tokens))
        charged = sum(tokens for at, tokens in events if at > now - 60)
        rows.append(
            {
                "logical_time_s": now,
                "requested_tokens": requests,
                "accepted": sum(accepted for accepted, _ in outcomes),
                "rejected": sum(not accepted for accepted, _ in outcomes),
                "charged_tokens": charged,
                "reservation_batch_ms": round(duration, 4),
            }
        )
    assert rows[0]["accepted"] == 2 and rows[0]["rejected"] == 1
    assert all(row["charged_tokens"] <= 50000 for row in rows)
    return {
        "limit_tokens": 50000,
        "window_seconds": 60,
        "timeline": rows,
        "clock": "injected logical seconds; real concurrent SQLite transactions",
    }


@dataclass
class TimedProvider:
    mode: str
    delay: float
    cancelled: bool = False

    async def complete(self, prompt: str, max_output_tokens: int) -> Completion:
        try:
            await asyncio.sleep(self.delay)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        if self.mode == "429":
            raise ProviderError(429)
        return Completion("measured mock output", self.mode, None)


async def fallback_benchmarks(directory: str) -> list[dict]:
    limiter = RateLimiter(str(Path(directory) / "fallback.db"), limit=1000000)
    await limiter.initialize()
    rows = []
    for label, mode, delay, repeat in [
        ("Primary success", "primary", 0.01, 10),
        ("HTTP 429 fallback", "429", 0.01, 10),
        ("3000 ms timeout fallback", "timeout", 5.0, 3),
    ]:
        samples, cancelled = [], []
        for index in range(repeat):
            primary = TimedProvider(mode, delay)
            backup = TimedProvider("backup", 0.005)
            router = ModelRouter(limiter, primary, backup, timeout=3.0)
            started = time.perf_counter()
            result = await router.complete(f"{mode}-{index}", "benchmark", 64)
            samples.append((time.perf_counter() - started) * 1000)
            assert result.model == ("primary" if mode == "primary" else "backup")
            cancelled.append(primary.cancelled)
        rows.append(
            {
                "scenario": label,
                **distribution(samples),
                "primary_fixture_delay_ms": delay * 1000,
                "backup_fixture_delay_ms": 5,
                "configured_primary_timeout_ms": 3000,
                "primary_cancelled_each_trial": cancelled,
            }
        )
    assert all(rows[-1]["primary_cancelled_each_trial"])
    return rows


def save_plots(report: dict) -> None:
    plt.rcParams.update(
        {
            "font.family": "DejaVu Sans",
            "font.size": 10,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "axes.titleweight": "bold",
            "figure.facecolor": "white",
        }
    )
    rows = report["streaming"]
    x = [str(row["chunk_chars"]) for row in rows]
    fig, axes = plt.subplots(1, 3, figsize=(13, 4.2), layout="constrained")
    measurements = [
        (
            [row["first_output"]["median_ms"] * 1000 for row in rows],
            "First safe output",
            "Microseconds",
        ),
        ([row["total"]["median_ms"] for row in rows], "Complete input processing", "Milliseconds"),
        ([row["peak_traced_bytes"] / 1024 for row in rows], "Peak traced allocation", "KiB"),
    ]
    for axis, (values, title, unit) in zip(axes, measurements, strict=True):
        bars = axis.bar(x, values, color="#2563eb", width=0.65)
        axis.bar_label(bars, fmt="%.2f", padding=3, fontsize=8)
        axis.set(title=title, ylabel=unit, xlabel="Input chunk size (characters)")
        axis.set_ylim(0, max(values) * 1.22)
        axis.grid(axis="y", alpha=0.15)
    fig.suptitle("Streaming guardrail | 114,000 characters | processing only", fontsize=14)
    fig.savefig(IMAGES / "streaming_performance.png", dpi=150)
    plt.close(fig)

    rows = report["quota"]["timeline"]
    fig, axis = plt.subplots(figsize=(10, 4.6), layout="constrained")
    bars = axis.bar(
        [str(row["logical_time_s"]) for row in rows],
        [row["charged_tokens"] for row in rows],
        color="#2563eb",
        width=0.65,
    )
    axis.axhline(50000, color="#dc2626", linestyle="--", label="50,000-token cap")
    axis.bar_label(
        bars,
        labels=[f"{row['accepted']} accepted\n{row['rejected']} rejected" for row in rows],
        padding=5,
        fontsize=9,
    )
    axis.set(
        title="Sliding-window quota: real SQLite transactions",
        xlabel="Request batch at injected second (categorical spacing)",
        ylabel="Tokens charged after each batch",
        ylim=(0, 65000),
    )
    axis.legend(loc="lower right")
    axis.grid(axis="y", alpha=0.15)
    fig.savefig(IMAGES / "token_rate_limit.png", dpi=150)
    plt.close(fig)

    rows = report["fallback"]
    fig, axis = plt.subplots(figsize=(9, 4.4), layout="constrained")
    bars = axis.bar(
        [row["scenario"] for row in rows],
        [row["median_ms"] for row in rows],
        color=["#2563eb", "#0891b2", "#ea580c"],
    )
    axis.set_yscale("log")
    axis.set(
        title="Model routing | controlled asynchronous providers",
        ylabel="Median completion time (ms, log scale)",
    )
    axis.bar_label(
        bars,
        labels=[f"{row['median_ms']:.2f} ms\nn={row['n']}" for row in rows],
        padding=5,
        fontsize=9,
    )
    axis.set_ylim(1, 10000)
    axis.grid(axis="y", alpha=0.15)
    fig.savefig(IMAGES / "model_failover.png", dpi=150)
    plt.close(fig)

    endpoints = report["endpoints"]
    names = ["MCP tool call", "Authorized proxy", "Blocked admin call"]
    values = [endpoints[key]["median_ms"] for key in ["mcp_call", "proxy_allowed", "proxy_blocked"]]
    fig, axis = plt.subplots(figsize=(9, 4), layout="constrained")
    bars = axis.barh(names, values, color=["#2563eb", "#0891b2", "#64748b"])
    axis.bar_label(bars, fmt="%.3f ms", padding=4)
    axis.set(
        title="SDK and ASGI round trips | in-process | 30 trials",
        xlabel="Median elapsed time (ms)",
        xlim=(0, max(values) * 1.35),
    )
    axis.invert_yaxis()
    axis.grid(axis="x", alpha=0.15)
    fig.savefig(IMAGES / "endpoint_latency.png", dpi=150)
    plt.close(fig)


async def main() -> None:
    IMAGES.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as directory:
        report = {
            "environment": {
                "python": platform.python_version(),
                "platform": platform.platform(),
                "logical_cpus": os.cpu_count(),
                "source_commit": os.getenv("GITHUB_SHA", "local"),
                "measured_at_utc": datetime.now(UTC).isoformat(),
            },
            "tests": test_counts(),
            "endpoints": await endpoint_benchmarks(),
            "streaming": streaming_benchmarks(),
            "quota": await quota_benchmarks(directory),
            "fallback": await fallback_benchmarks(directory),
        }
    save_plots(report)
    (OUTPUT / "metrics.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print("ASSESSMENT_METRICS=" + json.dumps(report))
    # Optional public-asset export for connected clients; normal CI uses artifacts.
    if os.getenv("EXPORT_REPORT_ASSETS") == "1":
        for path in sorted(IMAGES.glob("*.png")):
            print(
                "ASSESSMENT_PNG="
                + json.dumps(
                    {"path": str(path), "base64": base64.b64encode(path.read_bytes()).decode()}
                )
            )


if __name__ == "__main__":
    asyncio.run(main())
