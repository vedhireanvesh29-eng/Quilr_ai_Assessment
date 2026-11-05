# Implementation decisions

## SDK lifecycle and errors

Task 1 targets official Python SDK v2 (`mcp>=2,<3`), with MCP 2.3.0 in the lockfile. `mcp.server.MCPServer` and `@server.tool()` provide the registry, discovery schemas, validation and invocation. A public SDK `Server` runner delegates discovery and execution to the registry's public methods. SDK `stdio_server()` owns parsing and framing. The host starts a subprocess; stdin/stdout carry one protocol connection, while Python logging writes to stderr. The server never writes debugging text to stdout.

The SDK negotiates protocol compatibility. The subprocess test exercises the supported legacy sequence: `initialize` with version `2025-06-18`, response, `notifications/initialized`, then `tools/list` and `tools/call`. Newer SDK clients may use `server/discover`; application code does not implement either handshake itself.

Public v2 middleware inspects raw `tools/call` parameters before SDK tool invocation. Pydantic schemas reject incorrect types, extra fields, malformed IDs, boolean/nonfinite/nonpositive amounts, and short reasons. Middleware raises `MCPError(code=-32602, message="Invalid tool arguments")`. This is a top-level protocol error; it is distinct from a successful JSON-RPC envelope whose tool result contains `isError:true`. Unknown tools return `-32602`; absent customer records are business errors surfaced by the SDK's tool-error conversion. Unknown methods and parse errors belong to the SDK.

The SDK stdio parser yields Pydantic validation exceptions for malformed lines; its default runner logs these without a response. `transport.py` maps SDK parser exceptions to `-32700` for malformed JSON and `-32600` for invalid envelopes, with a null ID. Valid SDK-parsed messages pass through untouched to the public runner over an AnyIO memory stream with zero queue capacity. SDK protocol types serialize the error response. This adds error mapping without reimplementing wire parsing, initialization, dispatch or serialization, and without accessing private SDK attributes. Business-level tool failures are sanitized as tool results with `isError:true`.

The SDK documentation marks middleware provisional within v2. The lockfile and real subprocess wire test detect version drift. The locked SDK is installed and exercised in GitHub Actions because the local host cannot download dependencies.

## Proxy authorization

Bearer credentials map to roles using two configured development tokens. Body-supplied roles have no effect. `tools/list` goes through unchanged. A viewer's `admin_` tool call receives the required `-32001` error with the original ID before HTTPX is invoked. A reusable lifespan-scoped client forwards to a server-configured URL with redirects disabled. No untrusted headers or destination URLs are forwarded. Network errors become sanitized JSON-RPC gateway errors. Downstream JSON-RPC bodies are validated and then returned unchanged.

This is a JSON request/response proxy around a mock MCP endpoint, not a complete MCP Streamable HTTP session proxy. Authentication precedes parsing, so invalid credentials receive HTTP 401 even if their body is malformed.

## Incremental redaction

Independent substitution on chunks leaks values split across chunks. Instead, the redactor carries one lexical candidate across every `feed` call. Ordinary tokens finish at whitespace. Numeric candidates can span whitespace/hyphens to capture formatted cards and SSNs. Candidates are scanned only when safe to emit. Emails require an `@` and dotted domain; incomplete `@` candidates are also conservatively redacted. SSNs use a three-two-four digit shape, including unassigned combinations because this is a disclosure guard. Cards need 13–19 ASCII digits and a valid Luhn checksum.

At the configured buffer threshold, the candidate becomes `[REDACTED]` and the state machine discards its remainder until a safe delimiter. Retaining a fixed tail cannot make unbounded email matching safe; this fail-closed policy handles arbitrarily long candidates without releasing prefixes. Long legitimate identifiers may also be redacted. Memory is O(configured pending limit + current input/output chunk). The redactor intentionally processes complete candidates, so its practical latency is one word or numeric run; safe text emits before source completion.

The SSE parser incrementally decodes UTF-8, handles CR, LF, CRLF, comments, and multiline data, and caps event state at 65,536 characters. JSON delta payloads contain `text`. Only properly completed events reach the redactor. Truncation, malformed JSON/UTF-8, missing completion markers, or provider errors discard pending text. Downstream framing is reconstructed from safe deltas. Upstream response context managers close on success, failure, cancellation, or generator close. No producer queue is used, so downstream backpressure reaches the upstream iterator.

## SQLite window and concurrency

The events table records hashed tenant ID, unique request ID, timestamp, reservation, optional actual usage, and state. Indexes support tenant/time-window sums and expired-state cleanup. Every reserve opens a dedicated connection, uses a five-second busy timeout, acquires `BEGIN IMMEDIATE`, performs cleanup, sums charges, checks the limit, inserts, and commits. Independent connections and processes therefore cannot all spend the same remaining quota. Parameterized statements protect tenant values. WAL permits readers while a writer owns the reservation transaction. SQLite operations run in worker threads, avoiding synchronous disk I/O in the request coroutine.

Completed charges expire after a rolling 60 seconds measured from reconciliation. This is conservative for long generations. Active requests remain charged even if their start time has left the window. Reservations older than 120 seconds become full stale charges timestamped at cleanup, then expire after another 60 seconds. This bounds crashed-request retention without suddenly refunding unknown consumption. Defaults assume normal provider operations end well before the stale threshold. Production timeouts should preserve that relationship.

SQLite wall-clock timestamps survive process restarts. Test clocks are injected. Clock rollback conservatively retains more history; large wall-clock jumps are a limitation. Distributed hosts require synchronized clocks or a centralized rate service.

## Tokens, failover and reconciliation

The replaceable token counter defaults to UTF-8 byte length. The mock uses the same input accounting and bounds generated output by bytes. For real byte/BPE providers, the input byte count is a conservative text-token estimate; provider-specific framing and special tokens require a matching adapter. Reservations are input estimate plus requested maximum output. Reported successful primary usage reconciles the charge with a floor of observed input/output under the configured counter; usage greater than reservation consumes additional quota for future checks. A provider must honor its output limit for reservations to guarantee admission safety.

There is one quota reservation per logical request. Primary 429 and a three-second timeout trigger exactly one backup attempt. `asyncio.timeout` cancels and awaits the primary task before backup invocation, avoiding a timeout race with a late response. Arbitrary 500 errors and connection failures do not trigger fallback. Backup has a bounded timeout too. Responses are nonstreaming, so failover cannot duplicate partially committed content.

Failed/aborted primary attempts may have consumed unknown tokens. Fallback retains at least the full reservation and any larger observed or reported backup usage. Failures with unknown usage retain the reservation; neither path refunds the primary attempt based only on a smaller backup receipt. This implements a logical-request quota, not a precise two-provider billing ledger. Actual usage across both attempts would need provider receipts for strict cost accounting. Reconciliation runs in a shielded task and is awaited on cancellation. If the host crashes or reconciliation fails, the existing pending reservation remains charged until stale handling.

## Security and performance assumptions

Development keys are not production identity. Tenant SHA-256 hashes avoid storing raw keys but do not turn low-entropy keys into secrets. HTTP mock services are localhost-only by default. Error payloads never include provider bodies, database paths, or exception traces. Request validation on the router also uses the standard sanitized schema.

SSE events and redaction candidates are bounded; endpoints still need deployment-level request limits and connection quotas. SQLite serializes short write transactions; that is appropriate for this task, with bounded busy waits and no process-local quota source of truth. Database cleanup is indexed but remains shared writer work. High-volume deployments would need measured retention tuning and a centralized store.


## Requirements review fixes

See [REQUIREMENTS.md](REQUIREMENTS.md) for the assessment-to-test mapping. Numeric candidates now include attached labels and punctuation before the first digit group; numeric groups following an oversized token remain suppressed. Incomplete SSN-shaped values are redacted at EOF, and the SSE decoder accepts an initial UTF-8 BOM.

The default token counter is also applied to observed output. Reconciliation cannot charge less than observed input plus output under that counter, even if a provider reports a smaller number. A fallback keeps at least the original reservation and records larger reported or observed usage, without creating a second reservation. Late completion after stale cleanup restores its attributed charge. Repeated reconciliation leaves an already completed event unchanged. UTF-8 byte counting remains conservative and replaceable; real-provider tokenizer/framing compatibility is still an explicit deployment requirement.

The launcher probes ports before starting, waits for readiness, monitors every child, returns nonzero on startup failure, and closes its subprocesses. Windows shutdown terminates the owned process tree because venv Python can act as a redirector. The live verifier uses isolated ports, a temporary on-disk database, and the official SDK client against a real stdio subprocess. It then makes real TCP HTTP calls. Local-only clients do not consult external proxy variables; clients with remote configured endpoints continue to honor them.

Gateway failure logs record only standardized codes, request IDs and HTTP statuses. They do not log prompts, API keys, upstream response bodies or raw exception messages. Gateway host and base port are configurable through `GATEWAY_HOST` and `GATEWAY_PORT`, or demo CLI flags.
