# Requirements and executable evidence

Paths below are relative to [src/ai_field_assessment](src/ai_field_assessment) and [tests](tests). The four services use development credentials and local mock providers.

| Requirement | Implementation | Evidence |
|---|---|---|
| Two discoverable MCP tools and SDK lifecycle | `task1_mcp_server/server.py`, `transport.py` | `test_mcp_stdio.py`; live SDK subprocess client |
| Exact customer ID, strict amounts/reasons, no extra fields | `task1_mcp_server/schemas.py` | `test_schemas.py`; invalid wire calls |
| JSON-RPC parsing, envelope, method and argument errors | SDK runner and public transport adapter | Wire assertions for -32700, -32600, -32601 and -32602 |
| Customer lookup and simulated decimal refunds | `task1_mcp_server/customer_service.py` | Both tools over stdio; unknown-customer tool error |
| Bearer roles; stop viewer admin calls before forwarding | `task2_mcp_gateway/app.py` | Role tests; backend spy and real TCP authorization |
| Transparent discovery and request IDs | Proxy and `common/jsonrpc.py` | Discovery comparison; ID zero regression |
| Malformed JSON, tokens and downstream failure handling | Proxy validation and lifespan client | Nonfinite numbers, non-ASCII token, timeout and connection fixtures |
| Server-owned destination and no copied credentials | Explicit JSON-only upstream request | Forwarding spy and proxy source |
| Cross-chunk emails, SSNs and Luhn-valid cards | `task3_stream_guardrail/redactor.py` | Every split point, seeded layouts, prefixes, punctuation, whitespace separators |
| Bounded candidate memory and fail-closed overflow | Candidate cap and numeric continuation suppression | Long candidate, oversized prefix and large safe-stream cases |
| Incremental UTF-8 and SSE framing | `task3_stream_guardrail/stream_parser.py` | BOM, split multibyte data, CR/LF, multiline, malformed and truncated events |
| Early output, cancellation and upstream cleanup | Async guardrail iterator and response context | Gated first-output test; disconnect cleanup; live SSE |
| Configurable mock chunks and interrupted streams | `mock_llm_provider.py` | Chunk-size/truncation fixtures |
| Persistent per-tenant 50,000-unit rolling quota | `task4_model_router/rate_limiter.py` | Core exact/over-limit tests; tenant isolation; interpreter restart |
| Atomic concurrent admission | WAL, immediate transaction, shared disk | Three independent 20,000-unit requests admit two and reject one |
| Immutable reservation time and separate completion | `reserved_at`, `completed_at` | `test_rate_window.py`: completion delays 0, 10, 59 and 60; exact expiry |
| Pending charge past the window; bounded stale grace | Reservation-anchored pending/stale states | Boundary, failure, exact stale deadlines and late completion tests |
| Safe migration of existing SQLite files | Transactional version 2 copy and schema checks | Legacy disk fixtures, concurrent initialization, forced WAL lock contention, rollback and future-version refusal |
| Retained history and attributed late reconciliation | Frozen reservation context and separate retention | Purged history recovery with original time, metadata and receipt |
| Distinct estimates, reserves, receipts and charges | Router accounting and ledger columns | `test_token_accounting.py`: missing/underreported receipts, large output, overrun |
| No uncontrolled refund or duplicate fallback reserve | Observed floor and one logical-request ledger row | Primary failure, fallback success/failure, cancellation and idempotence tests |
| Primary first; one backup on 429 or deadline | `task4_model_router/router.py` | Success, 429, timeout cancellation, 500 and both-failed cases |
| Sanitized HTTP errors and diagnostic metadata | Router `failure()` | Status/code assertions and sensitive-data logging test |
| Managed local processes and cleanup | `demo.py`, `verification.py` | Occupied port, child exit, isolated ports, subprocess/TCP verifier, Windows shutdown |
| Reproducible checks and measured plots | Lockfile, CI, report script | Linux/Windows checks, JUnit, live verifier, raw JSON and PNG artifacts |

## Accounting contract

Completed quota uses the original 60-second reservation window. Pending rows remain charged until reconciliation or stale handling. Stale unknown usage is held until admission age 180 seconds, with no cleanup-time reset. A late receipt updates the ledger without opening another window.

Legacy timestamps cannot reveal reservation times overwritten by older completion logic. Migration retains their last known time and stored charges. Null usage means unknown usage, not zero. The UTF-8 counter estimates local text units; provider-specific tokenization and combined-provider billing need separate adapters and receipts.

## Evidence boundaries

The Task 2 HTTP endpoint is a single-request JSON-RPC proxy, without full MCP HTTP sessions. PII rules cover explicit lexical forms and can over-redact. Authentication, refunds, and providers are simulated. SQLite is a single-host quota store.

[README.md](README.md) links the test and measurement snapshot to its exact CI run and source SHA. Test totals count collected cases, not code coverage. Performance plots describe controlled fixtures rather than remote LLM benchmarks.
