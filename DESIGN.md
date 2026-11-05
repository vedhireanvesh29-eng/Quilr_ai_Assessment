# Design decisions

## Protocol boundary

Task 1 uses the locked MCP SDK v2 registry, discovery, validation, and invocation APIs. A public SDK `Server` runner delegates to the registry. The SDK stdio transport owns framing and parsing. A public adapter maps parser validation failures to JSON-RPC errors without replacing the parser or accessing private attributes.

Malformed JSON maps to `-32700`; invalid envelopes map to `-32600` with a null ID. Middleware validates raw tool parameters before invocation and returns `-32602` for invalid arguments or unknown tools. Unknown methods return `-32601`. A recognized customer-not-found exception becomes a sanitized tool result with `isError:true`.

The SDK negotiates protocol compatibility. Tests cover the legacy initialize/initialized sequence and a current SDK client connection. Middleware is provisional in SDK v2, so the lockfile and real wire tests matter when upgrading. Stdout carries protocol messages only.

## Authorization boundary

Task 2 maps configured Bearer credentials to roles before parsing JSON. Viewer discovery remains transparent, including administrative tools. Viewer `admin_` calls are rejected before forwarding, preserving the request ID.

The destination comes from server configuration. Redirects are disabled, and incoming headers are not copied to the backend. A lifespan HTTPX client reuses connections. This is a single-request JSON proxy around a mock endpoint; it does not implement stateful MCP Streamable HTTP sessions.

## Streaming state and resource use

The redactor carries lexical candidates between deltas. Ordinary candidates finish at whitespace; numeric candidates retain whitespace and hyphens so formatted cards and SSNs can be inspected as a whole. Attached prefixes remain buffered with the first numeric group. Cards require 13–19 ASCII digits and Luhn validation. SSNs use a three-two-four shape without checking issuance. Email-like incomplete values and incomplete SSNs are conservatively hidden at EOF.

At the configured candidate cap, the redactor emits one marker and discards the rest through a safe boundary, including numeric continuation groups. This can hide long legitimate identifiers. Pending state is bounded; total working memory also includes the current input and output chunk. Candidate buffering adds approximately a word or numeric run of delay rather than requiring the whole response.

The SSE parser incrementally decodes UTF-8 with optional initial BOM, handles CR/LF, comments and multiline data, and caps an event at 65,536 characters. Only complete events reach the redactor. Malformed or incomplete streams discard pending candidates. Async iteration provides backpressure without a producer queue. Response contexts close upstream connections on success, error, cancellation, or generator close.

## SQLite admission and time

Each reservation has an immutable `reserved_at` and a separate nullable `completed_at`. The ledger stores a hashed tenant, request ID, estimated input, reserved output, total reservation, optional provider receipt, optional reconciled charge, and state.

| State | Charge | Quota eligibility |
|---|---|---|
| Pending | Full reservation | Until reconciliation or stale transition, including after 60 seconds |
| Complete | Reconciled charge, otherwise reservation | Original reservation is younger than 60 seconds |
| Stale | Full reservation | Original reservation is younger than 180 seconds |

Pending rows become stale at age 120 seconds. Cleanup never changes reservation timestamps. An old row first observed at age 181 is already outside its stale grace period; cleanup does not start another minute. Late completion records its receipt and completion time, but cannot create a new current-window charge.

A reserve transaction acquires `BEGIN IMMEDIATE` before sampling the clock, ages pending state, prunes retained history, sums tenant charges, checks the budget, and inserts. WAL and a five-second busy timeout support independent connections. Parameterized statements protect tenant values. SQLite work runs in worker threads. Admission serializes writers, so two concurrent checks cannot spend the same remaining budget.

Operational history lasts 24 hours by default. Pruning requires both admission and completion to be outside retention and never removes pending rows directly. Retention is independent of the quota window. The router retains a frozen reservation context; reconciliation can restore a purged row using its original time and attribution. Repeating reconciliation leaves a completed row unchanged.

Time comes from a wall clock so state survives process restarts. Tests inject a logical clock without sleeping. Rollback of the clock retains extra charges conservatively. A large forward jump can expire charges early; distributed clocks and cross-host databases are outside this local design.

## Disk-schema migration

Initialization takes an immediate transaction and checks `PRAGMA user_version`. Version 2 stores separate reservation and completion timestamps. A legacy `timestamp` table is renamed, copied into the constrained version 2 table, and dropped inside one transaction. IDs, tenants, reserved/actual amounts, and states are preserved. Indexes and the schema version are committed together. Invalid legacy rows roll the transaction back; future schema versions are refused.

Legacy completion code overwrote `timestamp`. Its original reservation time cannot be recovered. Migration uses the last stored time as `reserved_at`, also using it as `completed_at` for completed rows. This can hold a legacy completed charge longer than its unknowable original window. New rows keep both times correctly. Estimate and receipt columns remain null for migrated rows because those facts were not stored.

Initialization is repeatable. Disk tests cover concurrent initialization, retained quota, exact migrated values, whole-migration rollback, and refusal to downgrade a newer schema. Back up an operational database before upgrading.

## Token estimates, receipts, and fallback

The default input and observed-output counter is UTF-8 byte length. It is a local approximation rather than a provider-independent token count. A replaceable counter allows a provider adapter to use its own tokenizer and include framing. Admission reserves input estimate plus maximum output.

A successful receipt is stored separately from the quota charge:

- With a receipt, primary success charges at least the larger of observed usage and reported usage.
- Without a receipt, the full reservation remains, or a larger observed amount is charged.
- Fallback retains at least the original reservation, observed usage, and any reported backup usage.
- Failure or cancellation without a receipt retains the reservation.

A provider can report more than the reservation; that amount reduces future availability even when it exceeds the cap. A provider must honor its output budget and token contract for admission estimates to bound future usage. No reservation scheme can retroactively prevent an unbounded provider response.

There is one reservation per logical request. The router calls primary first, cancels and awaits it at the configured deadline, and calls backup once only for 429 or timeout. Backup also has a deadline. Other errors do not trigger fallback. Returning a single completion avoids replaying partially delivered content.

Unknown failed-primary usage is not separately measurable. Keeping one reservation prevents an unconditional refund, but does not equal an exact two-provider billing ledger. A smaller backup receipt cannot erase the reservation. Known overrun is retained without adding the same reservation twice.

Reconciliation runs in a shielded task and is awaited during cancellation. A crash or database failure leaves the pending reservation for stale handling. A task cancelled while admission runs in a worker thread can also leave a pending row; this favors retained quota over an untracked provider call.

## Errors and local execution

Router failures use standardized code/message/request-ID payloads. Logs contain only those IDs, codes, and status numbers, without prompts, keys, upstream bodies, or exception messages. Development keys identify tenants without authenticating them; SHA-256 storage does not make weak keys secret.

The demo owns five subprocesses, probes ports, waits for readiness, monitors child exits, and closes owned process trees. Verification uses isolated ports and a temporary disk database. Local-only configured URLs bypass environment proxies; remote configured URLs retain proxy support.

## Measurement boundaries

[scripts/benchmark_report.py](scripts/benchmark_report.py) reads actual JUnit cases and records raw samples, environment, source SHA, and UTC time. MCP and proxy measurements are in-process round trips. Streaming measures redactor processing and a separate allocation pass. Quota trials run real concurrent SQLite writes with an injected clock. Failover trials include the real three-second primary deadline.

These fixtures measure local implementation behavior. They do not measure remote vendor latency, whole-process memory, production capacity, or model generation throughput. Timing thresholds are not used as CI assertions. [REQUIREMENTS.md](REQUIREMENTS.md) maps implementation decisions to executable evidence.
