# Verification

Application revision: [9b695b7](https://github.com/vedhireanvesh29-eng/Quilr_ai_Assessment/commit/9b695b73d126220bbe4ea17274c4a421438393af).

Local Linux checks completed with Python 3.12.14:

- `uv sync --locked`: passed.
- `uv run ruff check .`: passed.
- `uv run ruff format --check .`: passed, 42 Python files.
- `uv run pytest -v --junitxml=results.xml`: 106 passed, 0 failed, 0 skipped.
- `uv run python scripts/verify_all.py`: all nine stdio/TCP checks passed.
- `uv run python scripts/benchmark.py`: completed.
- `uv run --with matplotlib==3.10.7 python scripts/benchmark_report.py`: completed; raw measurements and four PNGs regenerated.

[GitHub Actions](https://github.com/vedhireanvesh29-eng/Quilr_ai_Assessment/actions/runs/42120304096) checks Linux and Windows. [Raw measurements](../docs/metrics.json) record the source SHA, environment, timestamp, samples, and per-task counts. [Requirements](../REQUIREMENTS.md) map behavior to tests.

Measurements use local mocks and real SQLite operations. They do not represent remote-provider performance. Test totals count collected cases rather than code coverage.
