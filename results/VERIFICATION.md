# Verification status

The project is verified in GitHub Actions using Python 3.12, the official MCP SDK 2.3.0, and dependencies from `uv.lock`. The final workflow enforces dependency locking, lint, formatting, all tests, and the benchmark.

Executed locally on November 4, 2026:

- Six synchronous redaction/JSON-RPC tests: passed, including every split point and 100 seeded randomized layouts.
- Two Pydantic schema tests: passed using the bundled Pydantic runtime.
- Independent SQLite connections in three concurrent worker threads: exactly two 20,000-token reservations accepted against a 50,000-token limit; one rejected. Cleanup completed after fixing initialization connection closure.
- Python compilation of all source, scripts and tests: passed.
- Redactor processing benchmark: completed; see the JSON and SVG in this directory. Input length is 114,000 characters. These measurements exclude provider/network time.

GitHub Actions verification on November 4, 2026: **37 tests passed**, including the real stdio subprocess, initialization, discovery, successful tools, strict argument errors, malformed JSON/envelopes, downstream authorization, streaming cancellation, UTF-8 SSE parsing, SQLite concurrency, quotas, and primary/backup failures. Ruff lint and formatting checks passed. Dependency installation with `uv sync --locked` and the benchmark also passed.

The complete suite ran in [GitHub Actions](https://github.com/vedhireanvesh29-eng/Quilr_ai_Assessment/actions). Local asynchronous tests were blocked because the restricted Windows host cannot create asyncio's loopback socket pair; PyPI downloads also failed locally. These host restrictions did not prevent clean-environment CI verification.

A written test is not counted as passing until executed.
