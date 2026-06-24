# Crawfish Security Spine

The six invariants that hold an agent's untrusted data away from its consequential
actions.

Security is a **spine, not a phase** — enforced on every feature from day one.

## The invariants

!!! warning "The prompt-injection boundary"
    `Flow.FLUID` values are **untrusted session data**. They reach the model as data,
    never as instructions. Consequential Sink targets and idempotency keys are
    **static-only**. A compromised item can never redirect a write or forge an
    idempotency key.

1. **Fluid inputs are untrusted session data (the prompt-injection boundary).**
   `Flow.FLUID` values (a ticket body, a diff an agent produced) reach the model as
   *data*, never concatenated into instructions. `Flow.STATIC` values are set once at
   batch start. Typing distinguishes the two (`crawfish.core`); the Definition
   compiler/runtime enforces the boundary (M1).

2. **Consequential Sink targets are static-only.** A Sink's *destination* (repo,
   project, channel) comes from `Flow.STATIC` config — never from fluid, model- or
   data-derived values. A compromised item cannot redirect a write.

3. **Idempotency keys derive from static config.** `key = hash(batch_id, item_id,
   static_sink_config)`; the check-then-write is a single transaction
   (`SqliteStore.claim_idempotency`, `INSERT OR IGNORE`) — no race under concurrency.

4. **Secrets matched to nodes; never logged or in-prompt.** `.env` is gitignored;
   a node receives only the secrets it declares (least privilege — the embryonic
   capability manifest). Credentials resolve **by reference**, never in `config`.
   Transcripts are scrubbed.

5. **Host-side node code runs out-of-process; taint propagates from fluid inputs.**
   Any value derived from a fluid input stays tainted. A tainted value cannot silently
   become a static Sink target or an idempotency key.

6. **Supply chain.** `crawfish.lock` carries integrity hashes; install-time
   capability consent gates what a plugin may touch.

!!! warning "Secrets resolve by reference"
    A node receives only the secrets it declares, resolved **by reference** — never in
    `config`, never logged, never in a prompt. Transcripts and telemetry are scrubbed
    before the Store write.

## Implementation status (Phase 1)

Shipped: static-vs-fluid typing (`crawfish.core`) + prompt-compiler boundary
(`runtime/prompt.py`); static-only Sink targets + idempotency keyed on **stable
per-item lineage + static config only** (never the random output id or model output)
with the approval gate evaluated *before* the claim (`nodes/sink.py`); taint
**originated** on fluid-source fan-out and on Runs with fluid inputs, propagating
through `Output.derive`/lineage (`nodes/source.py`, `run.py`); credentials by
reference + `.env` loader + node↔secret least-privilege mapping (`crawfish.secrets`);
transcript/telemetry redaction before the Store write (`ScrubbingStore`); install-time
capability consent + full-digest lockfile integrity (`craw install` / `craw freeze`);
out-of-process host-side execution + an egress-allowlist primitive (`crawfish.sandbox`).

Deferred (tracked separately): egress-mediated secret *injection* (a local
CommandRuntime can still read `.env` in-sandbox — the known v1 tradeoff); transparent
egress *interception* (the broker is a cooperative `guard()` allowlist today, not a
network chokepoint) and runtime enforcement of the consented capability manifest;
full microVM/seccomp hardening beyond out-of-process isolation.

## The operate/observe layer

The always-on layer inherits the spine above and adds four operate-specific guarantees.
It covers [deploy](../guide/deploy.md), [observers](../guide/observers.md),
[visualize](../guide/visualize.md), [manage](../guide/manage.md), and
[export](../guide/claude-code-export.md).

1. **Scrubbed observer events & run-info.** `ObserverEvent` and `RunInfo` are written
   through `ScrubbingStore` (reused, not reinvented) before the Store write, so no secret
   value reaches an event, the dashboard, `craw manage logs`, or a log file. Every row
   carries `org_id`.

2. **No-secret detached processes.** The `craw deploy` supervisor keeps secrets **by
   reference**, exactly as a foreground run: no credential in argv, the session name
   (`crawfish/<pipeline>`), the detached environment, the deploy registry row, or the
   supervisor log.

3. **Loopback-only dashboard.** `craw visualize` binds `127.0.0.1` only — no off-host
   surface — and renders only the scrubbed run-info surface.

4. **Cost-capped LLM observers.** A Definition-backed observer judge runs under the same
   `CostBudget`/`CostMeter` and the same static-vs-fluid prompt-injection boundary as any
   other Definition. Run data is **data**, never instructions; spend is capped and
   telemetered.

The [`craw export --claude-code`](../guide/claude-code-export.md) output carries **no
secrets** — it maps tool/MCP *references* only (the `tools` allowlist), never an `auth`
reference or a credential value, so the generated file is safe to commit.

## Agent-language foundations (Milestone F)

The foundational primitives behind the agent-language operators uphold the spine and add
five guarantees. Identity additions are recorded in
[ADR 0019](decisions/0019-content-hash-version-bump-and-migration.md).

1. **Tenancy enters run identity.** `org_id` now folds into the replay cassette `_key`
   (F-1, when `!= "local"`) and is carried on **every** `ledger_loop` row (F-2). Two
   tenants can no longer collide on a cassette, and a resume in org `b` sees **none** of
   org `a`'s completed loop iterations — cross-tenant resume cannot replay another org's
   work (`test_cross_org_isolation`, `test_depth_cross_org_isolation`).

2. **Correction-corpus poisoning is gated (Security Gap S4).** Corrections feed
   guards/verifiers as **ground truth**, so a poisoned corpus is an attack surface
   (F-4). Every `correction` emission declares its `provenance` (`TRUSTED` / `UNTRUSTED`)
   — **who** authored it — and carries the existing `tainted` marker propagated from any
   FLUID-derived value (the corrected→guard path is taint-analyzed). The admission gate in
   `GoldenSet.from_corrections` is an **AND**: a correction becomes trusted ground truth
   **only if** `provenance == TRUSTED` **AND** `tainted is False`. Anything `UNTRUSTED`
   *or* fluid-tainted is **quarantined** — it stays on the ledger for audit but never
   gates anything. The AND is load-bearing: a fluid-derived value cannot become ground
   truth even if mislabelled `TRUSTED` (taint wins). `emit_correction` records every
   attempt (audit completeness); the trust decision is made at admission, not at write.

3. **The precision gate fails closed (the CL-2 safety inversion).** `eval.precision_gate`
   is an **absolute** decision-quality gate for consequential verifiers/guards/sinks. It
   **fails closed**: an un-benchmarked verifier (`baseline_exists is False`), no positive
   predictions, or measured precision below `min_precision` all **raise**
   `VerifierNotGated`. The old default was "admit unless proven bad"; the new default is
   **"reject unless measured good."** A consequential verifier or guard must pass
   `precision_gate` against a real baseline before it may gate anything.

4. **No decode field escapes run identity.** Every decode parameter enters the
   replay/version boundary (F-5): the tunable knobs (`temperature` / `top_p` /
   `sample_k`) via the Definition `version.sha`, and the per-call `decode_seed` via the
   F-1 cassette key. So two distinct decode settings can no longer replay identically — a
   silently-different decode cannot reuse another's cassette (closes the TS-8 hole).

5. **Mutable borrows are tenancy-scoped and Store-enforced.** The train-mode exclusive
   borrow (F-7) keys both its idempotency claim and its `borrow_lock` record on `org_id`,
   so a borrow held by org `a` never blocks or is visible to org `b`
   (`test_cross_tenant_does_not_block`). Enforcement lives in the Store (no in-process
   registry), so exclusivity holds across processes and survives the SQLite→Postgres swap
   — see [ADR 0018](decisions/0018-borrow-lifetime-semantics.md).

## Review gate

Every feature is audited against these invariants before it ships. The security reviewer
signs off before a Linear issue can move to `Done`; High/Critical findings **block**
completion. The final pass includes a prompt-injection red-team against the demo's fluid
inputs.

## See also

- [Architecture](ARCHITECTURE.md) — the three seams the spine rides on
- [Emission taxonomy](emission-taxonomy.md) — how `tainted` and `secret_lease` cross the ledger
- [Concepts → security boundary](../guide/concepts.md) — the boundary in the directory model
- [ADRs](decisions) — the decisions behind these invariants
