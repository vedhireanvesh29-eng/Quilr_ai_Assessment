"""Reproducible local redactor benchmark; network/provider latency is excluded."""

import json
import statistics
import time
from pathlib import Path

from ai_field_assessment.task3_stream_guardrail.redactor import StreamingRedactor


def benchmark() -> list[dict]:
    text = "Safe text then john@example.com and 4111 1111 1111 1111. " * 2000
    rows = []
    for chunk_size in [1, 16, 64, 256, 1024]:
        timings, firsts = [], []
        high_water = 0
        for _ in range(3):
            redactor = StreamingRedactor()
            started, first = time.perf_counter(), None
            for offset in range(0, len(text), chunk_size):
                safe = redactor.feed(text[offset : offset + chunk_size])
                if safe and first is None:
                    first = time.perf_counter() - started
            redactor.finish()
            timings.append((time.perf_counter() - started) * 1000)
            firsts.append((first or 0) * 1000)
            high_water = max(high_water, redactor.high_water)
        rows.append(
            {
                "chunk_chars": chunk_size,
                "input_chars": len(text),
                "median_total_ms": round(statistics.median(timings), 3),
                "median_first_output_ms": round(statistics.median(firsts), 3),
                "max_pending_chars": high_water,
            }
        )
    return rows


def save(rows: list[dict]) -> None:
    directory = Path("results")
    directory.mkdir(exist_ok=True)
    (directory / "redaction_benchmark.json").write_text(json.dumps(rows, indent=2) + "\n")
    maximum = max(row["median_total_ms"] for row in rows)
    elements = [
        '<svg xmlns="http://www.w3.org/2000/svg" width="760" height="340" viewBox="0 0 760 340">',
        '<rect width="760" height="340" fill="#f8fafc"/>',
        '<g font-family="Arial" fill="#0f172a">',
        '<text x="30" y="35" font-size="22">Streaming redactor: measured processing time</text>',
        '<text x="30" y="60" font-size="13">Median of 3 runs; '
        f'{rows[0]["input_chars"]:,} input characters; excludes network time</text>',
    ]
    for index, row in enumerate(rows):
        y = 90 + index * 43
        width = row["median_total_ms"] / maximum * 450
        elements.extend(
            [
                f'<text x="30" y="{y + 18}" font-size="13">{row["chunk_chars"]} chars/chunk</text>',
                f'<rect x="155" y="{y}" width="{width:.2f}" height="25" rx="3" fill="#2563eb"/>',
                f'<text x="{165 + width:.2f}" y="{y + 18}" font-size="13">'
                f'{row["median_total_ms"]} ms</text>',
            ]
        )
    elements.append("</g></svg>")
    (directory / "redaction_benchmark.svg").write_text("\n".join(elements) + "\n")


if __name__ == "__main__":
    results = benchmark()
    save(results)
    print(json.dumps(results, indent=2))
