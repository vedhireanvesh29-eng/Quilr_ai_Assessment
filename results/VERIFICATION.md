# Requirements-review verification

The review uses the original four-task assessment and the detailed Python implementation requirements.

- **83 passed, 0 failed, 0 skipped** on the measured Linux CI run.
- Local Windows execution: **83 passed**; Ruff lint and all 40 Python files' formatting checks passed.
- Linux and Windows CI run locked installation, lint, formatting, all tests, and the live verifier.
- The live verifier launches the real SDK stdio server plus five HTTP processes on isolated ports with a temporary on-disk SQLite database.
- Real checks cover discovery, customer lookup, simulated refund, viewer/admin policy, streamed PII removal, primary completion, backup completion, and token quota rejection.
- Child-exit, occupied-port, resource cleanup, released-port, cancellation, stale reconciliation and separate-interpreter persistence regressions are included.

[Measured run](https://github.com/vedhireanvesh29-eng/Quilr_ai_Assessment/actions/runs/42109900861)
[Raw per-task counts and performance samples](../docs/metrics.json)
[Assessment-to-test mapping](../REQUIREMENTS.md)

The committed performance plots are dated snapshots from the revision recorded in `docs/metrics.json`. They use controlled mocks and do not claim real-vendor latency. Five inherited duplicate cases were removed before adding independent regressions.
