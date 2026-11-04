# Verification status

The implementation was created on a Windows host with restricted network and loopback access. Package installation failed because PyPI DNS could not be resolved. Full HTTP/SDK tests require the declared dependencies and are also configured in GitHub Actions.

Executed locally on November 4, 2026:

- Six synchronous redaction/JSON-RPC tests: passed, including every split point and 100 seeded randomized layouts.
- Two Pydantic schema tests: passed using the bundled Pydantic runtime.
- Independent SQLite connections in three concurrent worker threads: exactly two 20,000-token reservations accepted against a 50,000-token limit; one rejected. Cleanup completed after fixing initialization connection closure.
- Python compilation of all source, scripts and tests: passed.
- Redactor processing benchmark: completed; see the JSON and SVG in this directory. Input length is 114,000 characters. These measurements exclude provider/network time.

The full asynchronous test run was blocked locally: Windows asyncio initialization waits for a loopback socket pair, which the restricted host does not permit. SDK/HTTP integration and Ruff checks require dependency installation. GitHub Actions is configured to run these checks in a clean Ubuntu environment; consult the workflow run for its actual status.

A written test is not counted as passing until executed.
