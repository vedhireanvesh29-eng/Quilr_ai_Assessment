# Assessment requirements and executable evidence

Scope: the supplied four-task AI Field Developer assessment and its detailed Python implementation text. This checklist records implemented behavior, not a claim of production certification or a subjective 10/10 score.

| Requirement | Implementation | Executable evidence |
|---|---|---|
| Official MCP SDK, two discoverable tools | `task1_mcp_server/server.py`, public SDK registry/runner | `test_mcp_stdio.py`; `verification.verify_mcp` uses the SDK client against a subprocess |
| Exact customer identifier, strict amounts/reasons, extra-field rejection | `task1_mcp_server/schemas.py` | `test_schemas.py`, malformed wire calls in `test_mcp_stdio.py` |
| Numeric JSON-RPC errors rather than conflating tool errors | `server.py`, `transport.py` | Wire assertions for `-32700`, `-32600`, `-32601`, `-32602`; unknown customer is a distinct tool error |
| Stdio lifecycle, protocol compatibility, isolated stdout | SDK transport and runner; stderr logging | Legacy initialize sequence in wire test; current SDK connection in live test; every stdout response is parsed as JSON |
| Mock customer lookup and refund, decimal money | `customer_service.py` | Both tools invoked over real stdio; simulated refund ID/status inspected |
| Bearer admin/viewer policy, immediate admin denial | `task2_mcp_gateway/app.py` | Admin/viewer parameterized tests; backend spy proves zero calls on denial; real TCP verifier checks allowed and denied calls |
| Transparent discovery with IDs and params | `common/jsonrpc.py`, proxy forwarding | `test_list_is_transparent_and_admin_visible`; zero-valued ID regression |
| Malformed requests, tokens, downstream errors | Proxy validation and HTTPX lifespan | Parse/parameter failures, nonfinite JSON, non-ASCII tokens, connection/timeout sanitization tests |
| Fixed upstream, no untrusted forwarded headers | Server-configured URL and explicit JSON-only HTTPX request | Proxy code and forwarding spy; no body-specified URL is used |
| Email, SSN and Luhn-card redaction across deltas | `task3_stream_guardrail/redactor.py` | Every split point, seeded random layouts, card prefixes/quotes/brackets, incomplete SSNs, long safe and ambiguous streams |
| Bounded pending state and fail-closed overflow | Redactor cap plus numeric continuation suppression | `test_long_candidate_fails_closed`, oversized-prefix and bounded-large-safe-stream regressions |
| Correct SSE and UTF-8 boundaries | `stream_parser.py`, incremental UTF-8-sig decoding | CR/LF, comments, multiline data, BOM, split multibyte bytes, truncated and oversized events |
| Early safe emission, backpressure and cancellation | Async response generator; upstream response context | Gated upstream test emits before source completion; cancellation test verifies upstream close; real TCP SSE verifier |
| Configurable mock streams and failure cases | Mock provider `chunk_size` and `truncate_after`; deterministic prompts | Normal/PII/Unicode gateway tests and malformed/disconnected upstream fixtures |
| Per-tenant 50,000-token rolling quota on disk | `task4_model_router/rate_limiter.py` | Exact/over-limit, tenant isolation, deterministic clock expiry, independent connections, separate interpreter restart |
| Atomic concurrent read/check/reserve | `BEGIN IMMEDIATE`, WAL, indexes, worker-thread I/O | Three concurrent reservations admit exactly two under the specified budget |
| Reserve, reconcile, and retain uncertain charges | `router.py`, limiter event state | Underreported usage floor, fallback overrun, failed charge, idempotence, late completion after stale eviction |
| Primary first; fallback only on 429 or deadline | `router.py`, HTTP provider adapter | Primary success, 429, cancellation at timeout, no fallback on 500, both failing; benchmark uses the real 3000 ms deadline |
| Standard sanitized errors and diagnostic metadata | Router `failure()` schema and metadata-only warning | HTTP status/code assertions; request IDs; no upstream exception text or key in responses/logging arguments |
| Independent local execution without paid APIs | Environment configuration, local providers, managed demo cluster | Live subprocess/TCP test; `scripts/verify_all.py`; all five services start on isolated ports |
| Demo startup and cleanup actually verified | `demo.py`, launcher readiness checks and child monitoring | Occupied-port rejection, child-exit failure, process/port cleanup assertions; Windows process-tree shutdown |
| Tests, lockfile, documentation and actual plots | `uv.lock`, README, DESIGN, report script, CI | Ruff, pytest JUnit, live verifier, and measured PNG artifacts on CI |

## Fixes made during this review

- Prevented a formatted card after `card=`, a quote, bracket, or other prefix from being emitted group by group.
- Kept oversized-token numeric continuations suppressed and redacted incomplete SSN-shaped values at EOF.
- Rejected non-string tool names, nonfinite JSON numbers, and non-ASCII mock tokens with defined errors.
- Prevented provider usage from refunding below the configured counter's observed input/output floor; retained larger reported fallback charges.
- Restored attributed late-completion charges after stale-event eviction and kept repeated reconciliation idempotent.
- Added UTF-8 BOM support and a deterministic gated test for early gateway emission.
- Replaced the pytest-only verification wrapper with live stdio and HTTP checks.
- Added readiness, fail-fast startup, child monitoring, cleanup, isolated verification ports, and Windows process-tree termination.
- Removed five inherited duplicate limiter cases from the router class. Test totals count distinct collected cases after that removal.

## Deliberate boundaries

Authentication and providers remain explicit mocks. The Task 2 endpoint is the assessment's JSON request/response proxy, not a complete stateful MCP Streamable HTTP gateway. Pattern-based redaction can conservatively over-redact; an equals sign is legal inside an email local part, so `email=john@example.com` can be treated as one email candidate. The replaceable default token counter uses UTF-8 bytes; a production provider needs a matching tokenizer and framing budget. SQLite is a local-host quota store. Performance evidence is dated and linked to the code revision that generated it.
