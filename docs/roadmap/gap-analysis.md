# Gap analysis — what developers expect that Crawfish doesn't yet ship

*Compiled 2026-06-19, after CRA-150. Benchmarks the current surface against peer
**agent frameworks** (LangGraph, CrewAI, AutoGen, LlamaIndex, Pydantic-AI, Mastra,
DSPy, OpenAI Agents SDK, Google ADK, Haystack), **durable-workflow engines**
(Temporal, Restate, Prefect, Dagster, Inngest, Trigger.dev, Hatchet, Windmill,
Airflow, BullMQ/Celery), and **LLMOps / gateways** (LangSmith, Langfuse, Helicone,
Braintrust, Arize Phoenix, LiteLLM, Portkey).*

These are **net-new** gaps — items already on the roadmap are excluded: extensible
Source/Sink adapters, visual graph editor (Phase 5), enterprise control plane
(Phase 6), cloud platform/RBAC (Phase 7), remote/distributed execution (Phase 8),
local models (Phase 9), the hub/marketplace (CRA-125), `craw new` generators
(CRA-148), anomaly detection (CRA-110), and failure alerting (CRA-146).

The biggest finding: the existing roadmap leans toward *reach* (adapters, cloud,
visual authoring) while the gaps developers hit **on day one** cluster in two areas the
roadmap underweights — the **model/runtime layer** (structured output, provider
fallback, caching, streaming) and **batch reliability primitives** (dead-letter,
fairness, timeouts, cancellation). These are the adoption-blocking gaps. Suggested
home: a **Phase 1.6 — Reliability & Model Layer** epic (or fold into CRA-140).

Legend — **Fit**: ✅ extends an existing seam · ⚠️ needs new seam/runtime work · 🔒 has a
security-spine constraint to honor.

---

## Tier 1 — build first (universal table stakes, clean fit, adoption-blocking)

### 1. Structured / schema-constrained output (validate → re-prompt) — Critical ✅
Every peer agent framework ships this (`output_type` / `with_structured_output` /
`output_pydantic`). Today a node author hand-parses model text and writes their own
JSON-extract + retry-on-malformed loop. Crawfish already has a `TypeRegistry`, Pydantic
shapes, and typed static/fluid IO — a node/Definition declaring a Pydantic `output_type`
that auto-reprompts on validation failure is a natural extension of the type substrate.
The fluid/static split *strengthens* it: the schema is static, the content is fluid.

### 2. Multi-provider runtime + automatic fallback — Critical ✅🔒
Runtimes are Anthropic-centric (`claude -p` / managed / mock). A provider outage,
rate-limit, or cost ceiling has **no graceful degradation** — and for *bulk* fan-out a
rate-limited primary stalls the whole run (the #1 batch-failure mode). A `FallbackRuntime`
wrapping an ordered list of `AgentRuntime`s, switching on rate-limit/5xx/timeout, is
exactly what the `AgentRuntime` seam exists for. Only Pydantic-AI and Mastra treat
*fallback* (not just multi-provider) as first-class, so this is a **differentiator**, not
just catch-up. 🔒 Keep the injection boundary identical across providers.

### 3. Dead-letter queue + non-retryable error class + retry jitter — Critical ✅
The single highest-value reliability gap; every workflow engine treats a DLQ as table
stakes. When an item exhausts retries it must land in a durable parking lot — not vanish
or fail the whole batch. Crawfish's fan-out model makes poison items *especially* likely.
The event ledger already records terminal failures; add a `dead_letter` ledger state +
`craw manage retry --failed` over the existing item cursor. Bundle two cheap companions:
a `TerminalError` exception the worker routes straight to DLQ (skip the retry budget on a
4xx/auth/bad-input), and **jitter** on the existing backoff (avoid thundering-herd on a
recovering downstream).

### 4. Per-key / per-tenant concurrency + fairness keys — Critical ✅🔒
Today the worker pool is one global semaphore (`max_concurrency=8`). A single
tenant/customer/API-account can starve all others, and work against the same external
resource isn't serialized. Hatchet/Inngest/Trigger.dev make a **concurrency key** (→ a
virtual queue per key, with round-robin fairness) a headline feature. This complements the
existing `org_id` tenancy column and directly serves Crawfish's multi-tenant bulk domain.
🔒 The key must derive from **static config or trusted fields, never FLUID input**.

---

## Tier 2 — high value, strong fit

### 5. Production LLM response cache (exact + optional semantic) — High ✅
Caching today is **replay-only** (test fixtures). Gateways (Helicone/Portkey/LiteLLM)
ship a production response cache because for *bulk* work it's a major cost lever — a
re-run over an overlapping item set re-pays for identical calls. A content-addressed cache
keyed on (model, prompt, params) rides the existing `ArtifactStore`/`Store` seam. 🔒
cache entries are scrubbed like any stored transcript.

### 6. Streaming step + token events (live progress) — High ⚠️
`craw logs` and the dashboard are post-hoc; a long bulk run with no live stream feels
frozen and can't be early-cancelled on a bad trajectory. Step-event streaming is a clean
extension of the OTel-shaped spans already written to the Store. Token streaming is the
harder seam (the `claude -p` subprocess buffers) but belongs on the `AgentRuntime`
protocol. Pairs with #7 and #16.

### 7. In-flight cancellation (cooperative) — High ✅
`RunContext` already carries a `CancelToken`; wire `craw manage cancel <run>` to flip a
ledger flag the worker pool checks at item boundaries — a graceful, ledger-recorded cancel
of one in-flight Run (with cleanup), distinct from `craw manage stop` halting the
supervisor. Matches the checkpoint architecture exactly.

### 8. Per-step timeouts + heartbeats / zombie detection — High ✅
Crash reconciliation exists, but a *hung-but-alive* item (wedged API call, infinite loop)
blocks a fan-out slot forever, and there's no liveness signal. Add a `timeout_s` on a node
plus a heartbeat column the worker bumps, reaped by the supervisor through the existing
reconciliation loop.

### 9. OTel export over OTLP to external collectors — High ✅
Spans are already **OTel-shaped** but only written to the local Store. A thin OTLP exporter
(opt-in) lets teams ship traces to Jaeger/Tempo/Datadog/Langfuse with near-zero new
modelling — a cheap, standards-aligned win that plugs Crawfish into existing observability
stacks. 🔒 export the scrubbed spans only.

### 10. Retrieval / RAG: a `VectorStore` seam + embeddings + semantic nodes — High ✅
The tagline is "agents over **your data**," yet "memory" is exact-key KV, not semantic.
Anything needing "find relevant context across my corpus" forces a hand-bolted vector DB.
Add a `VectorStore` protocol alongside `Store`/`ArtifactStore`, embeddings as a runtime
call, and semantic retrieval as a Source/Filter node. The most strategically important gap
relative to the positioning. (Distinct from the roadmapped Source/Sink *adapter* work.)

### 11. Delay / debounce / throttle triggers — High ✅
Webhooks (a primary Crawfish trigger) are bursty: 10 events for the same entity in 2s
should debounce to 1 run; calls to a rate-limited downstream should throttle; one-off work
should be schedulable without a cron entry. All ride the Store seam (a keyed "pending until
quiet for N" row; a `run_after` timestamp the supervisor polls). Throttle overlaps the
rate-limit half of #4.

---

## Tier 3 — medium / polish / latent wins (cheap because primitives already exist)

### 12. Session / conversation threads — Medium ✅
A keyed, ordered message history in the Store, replayed into the next turn — a thin
abstraction over the existing SqliteStore + event ledger + durable Run, for multi-turn
refinement / back-and-forth flows.

### 13. Tool auto-schema from typed functions + MCP client — Medium ✅🔒
Peers derive a tool's JSON schema from a function signature + docstring for free; MCP
*client* support is now baseline. Crawfish models `MCPConnection` already — verify the
current tool-authoring DX, then close the gap. **Security synergy**: MCP/tool calls
subjected to the egress allowlist + out-of-process sandbox + taint propagation is a genuine
selling point vs peers who bolt MCP on with no isolation.

### 14. Prompt registry surface (diff / pin / rollback) — Medium ✅
Definitions are *already* directory-compiled and versioned (`Version` + `Freezable`).
Surfacing prompt-level diff/pin/rollback is mostly an inspector/CLI affordance over
existing artifact versioning — a low-cost win that makes prompt regressions visible.

### 15. Backfill / replay of historical windows — Medium ✅
`craw run --backfill <range>` materializing runs over a time/parameter window through the
ledger, with a reprocessing mode (failed-only vs all), modelled on Airflow. Distinct from
DLQ retry (#3): backfill re-drives a deliberate *range*, not just failures.

### 16. Live in-flight step observability — Medium ✅
The ledger already has per-step checkpoints + item cursor; surface them live in the
dashboard and via `craw manage logs --follow`. Mostly a read-side feature; OTel step spans
(#9) make it standards-aligned.

### 17. Signals / events into a running pipeline — Medium ✅🔒
Generalize the existing human-approval suspend/resume into `wait_for_event(key, match)`
backed by the Store and resumed by the webhook trigger — unlocks event-driven choreography
(wait for a dependency, a correlated webhook, a sibling run). 🔒 correlation keys static;
event payload is FLUID (data only).

### 18. In-flight version drain / migration policy — Medium ✅
Crawfish is *ahead* here (the ledger already version-pins). The remaining gap is an explicit
drain policy: route new runs to new code while in-flight runs finish on the version they
started (or pause-and-resume onto a new version, Restate-style). Rises to High once users
redeploy frequently against long batches.

### 19. Online guardrails on live traffic — Medium ✅
Scrubbing + the Observer primitive partially cover this. Add schema-validity / PII /
jailbreak checks as guardrail nodes that can block or flag a live run (not just post-hoc
judge it), composing with the existing injection boundary.

### 20. Reflection / planning node patterns — Medium ✅
Handoffs/delegation are already covered (hierarchical lead/sequential coordination). Missing
are composable "draft → critique → revise" and "plan → execute" node patterns over the
existing Router/Aggregator atoms — library-level, not architectural.

---

## Recommended first epic — "Phase 1.6: Reliability & Model Layer"

The four Tier-1 items form one coherent, adoption-unblocking epic, all clean-fit and
net-new:

1. **Structured output** (#1) — leverages the `TypeRegistry`.
2. **FallbackRuntime + multi-provider** (#2) — the `AgentRuntime` seam's purpose; bulk
   rate-limit resilience; a differentiator.
3. **Dead-letter + `TerminalError` + jitter** (#3) — one cohesive failure-handling feature.
4. **Per-key/tenant concurrency + fairness** (#4) — extends `org_id` + the worker pool.

Then a fast follow-on of the cheapest high-leverage Tier-2 items that mostly reuse existing
seams: **OTLP export** (#9), **production response cache** (#5), **in-flight cancellation**
(#7), and **timeouts/heartbeats** (#8).
