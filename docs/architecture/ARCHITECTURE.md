# Crawfish Architecture

How Crawfish turns a directory of files into typed, swappable runtime parts.

## The model

An **agent is a directory**. Write markdown (instructions and skills) and Python (tools,
typed IO); the framework **compiles** the directory into typed runtime objects. The
control-flow model:

```
Source → Filter → Batch(Definition) → Aggregator → Router → Sink
              ├─ fan-out:    one Run per item        (map)
              ├─ Aggregator: N Outputs → one         (reduce)
              └─ Router:      branch by label         (branch)
```

## Three swappable seams

The product model imports **none** of these directly — only their protocols. That is what
makes moving to cloud and scale a driver swap, not a rewrite.

| Seam | Protocol | Local default | Later |
|------|----------|---------------|-------|
| `AgentRuntime` | the agent loop/backend | CommandRuntime (`claude -p`) | ClientRuntime / ManagedRuntime (CMA) |
| `Store` | persistence | `SqliteStore` (WAL) | Postgres |
| `ArtifactStore` | blobs | local filesystem | S3 |

## Foundation (M0, shipped)

- **`crawfish.core`** — typed-IO atoms: `Flow` (STATIC/FLUID), `Parameter`, `Node`,
  `NodeKind`, `Policy`, `RunContext` (with `CostBudget` + `CancelToken`), and
  `parameters_compatible`. `crawfish.core.context.RunContext` carries the `org_id`
  tenancy key, defaulted `"local"`.
- **`crawfish.typesystem`** — a structural `TypeRegistry`: `Parameter.type` resolves
  to a registered type (primitive / record / `list[X]` / `Optional[X]`), with
  covariance, record width-subtyping, and JSON-Schema export.
- **`crawfish.versioning`** — `Version` (`0.1-sha` / `0.2`) + `Freezable`; a frozen
  artifact rejects mutation.
- **`crawfish.store`** — the `Store` protocol + `SqliteStore` (WAL, tenancy key,
  transactional `INSERT OR IGNORE` idempotency, append-only event ledger).
- **`crawfish.engine`** — the bootstrap that runs a pipeline of steps end to end
  under one `RunContext` (a no-op pipeline is valid). The richer typed `Workflow`
  builds on this.
- **`crawfish.config`** — `crawfish.toml` manifest + profile resolution
  (`dev`→command, `prod`→managed).

## Emission stream (Phase 2 observability)

- **`crawfish.emission`** — one typed signal, `Emission`. Every producer writes it onto
  the append-only event ledger, and every consumer (inspector, dashboard, anomaly engine)
  reads it. It rides the existing `Store.append_event` transport, so there is no new
  persistence seam and `ScrubbingStore` redaction still applies on write.
- The envelope and the **closed** `EmissionKind` taxonomy (10 kinds: `run_start`,
  `run_finish`, `model`, `tool`, `sink`, `compaction`, `observer`, `metric`,
  `secret_lease`, `jail_violation`) are a frozen contract — see
  [`emission-taxonomy.md`](emission-taxonomy.md) and
  [ADR 0013](decisions/0013-emission-taxonomy-and-inline-output-value.md). Each kind's
  required `attrs` keys are pinned in `REQUIRED_ATTRS`; `EMISSION_SCHEMA_VERSION` lets
  the ledger evolve.
- `emit(store, e, *, max_per_run=...)` writes an emission (with a lightweight per-run
  volume cap as a flood/DoS guard); `read_emissions(store, run_id)` reads them back.
  `Emission.from_event` is a **back-compat shim**: it lifts both new typed emissions
  *and* the legacy loose telemetry dicts older runs wrote (mapping `runtime.run` →
  `model`, run-lifecycle spans → `run_start`/`run_finish`, `sink.write` → `sink`,
  `context.compaction` → `compaction`, `ObserverEvent` dumps → `observer`; anything
  unrecognized lifts into a generic `metric` carrying the raw payload), so old runs
  stay inspectable.
- **Security:** `tainted` carries the fluid/untrusted marker across the emission
  boundary. Every emit site that holds an Output sets it from the producing
  `Output.tainted`. Emissions never carry secret values — `secret_lease` carries the
  `ref` only, and the ledger is written through `ScrubbingStore`.

## Typed outputs & validation (Phase 2)

- **`crawfish.validation`** turns a Definition's declared `outputs` / `inputs`
  (`list[Parameter]`) into a real type contract. `validate_output(text, outputs, reg)`
  parses the model's text and validates it against the schema; `validate_inputs(values,
  schema, reg)` checks bound input *values* (not just presence, which `run.validate()`
  did); `structural_diff(before, after)` is the order-canonical diff eval scoring and the
  tuner key off of. Validation is **registry-driven** — it walks the resolved `TypeDef`
  (PRIMITIVE / RECORD / LIST / OPTIONAL) from `crawfish.typesystem`, so there is **no new
  runtime dependency** (no `jsonschema`).
- **Extraction (parse-from-text).** `claude -p` (CommandRuntime) has no JSON mode and
  returns free text, so `validate_output` extracts JSON *out of* the text. It strips
  Markdown code fences and isolates the outermost `{...}` / `[...]` span before decoding.
  A single `str`-typed output (or a Definition with **no** declared outputs) is a
  pass-through: the raw text becomes `Output.value`, which keeps back-compat with the
  string-output era. Otherwise the parsed value is **canonicalised** (record keys
  sorted) so golden-set equality and diffs are deterministic under record/replay.
- **`Output.value` is the typed value, not a string.** On completion `run.py` builds
  `Output(value=<typed>, output_schema=definition.outputs, ...)` — a RECORD output yields
  a validated `dict`, a LIST a `list`, etc. (ADR 0013: the value is inline). `Metric`s
  and the inspector read it directly.
- **Failure reasons vs the action policy are distinct.** `ValidationFailure` is the
  **closed set of reasons** (`NOT_JSON`, `MISSING_FIELD`, `TYPE_MISMATCH`, `EXTRA_FIELD`,
  `EMPTY_SCHEMA`, `CONSTRAINT`) carried on each `ValidationError`. `ValidationAction`
  (`RETRY` / `REPAIR` / `DEAD_LETTER`) is the separate *policy* a `Run` applies when an
  output fails: `RETRY` re-runs via the existing `RetryPolicy`; `REPAIR` re-prompts the
  model **once** with the schema error fed back as fluid data (a metered extra call that
  respects `ctx.cost_budget` / `ctx.cancel_token`); `DEAD_LETTER` (default) gives up.
  The value is never silently coerced.
- **Security / taint.** The typed value is untrusted model output → `tainted=True` when
  any input was fluid **or** the run consumed any `tool_result` event (a malicious tool
  output is an injection vector). A wrong-typed input is rejected *before* any model call.
  Callers that deliberately over-bind (the `Router`'s classifier) opt out via
  `validate_input_types=False` / `validate_output_schema=False`.

## Schema migrations (Phase 2)

An older `.crawfish` database must upgrade cleanly when a newer binary opens it. The
schema version lives in SQLite's built-in `PRAGMA user_version`. `SqliteStore.__init__`
runs `crawfish.store.migrations.apply_migrations` under its lock: it applies every forward
migration whose version exceeds the on-disk version (each in its own transaction), then
stamps `user_version`. See ADR 0014.

- **Migration 1 is the baseline** — the original table set, written `CREATE TABLE IF NOT
  EXISTS`. So a brand-new DB and an existing *pre-versioning* DB (tables already present,
  `user_version=0`) both converge. Re-opening a current DB applies nothing (idempotent).
- **Downgrade is refused.** If `user_version` exceeds `CURRENT_SCHEMA_VERSION`, a newer
  binary wrote the DB; `apply_migrations` raises `StoreMigrationError` rather than risk
  corruption.
- **Concurrency** is safe: migrations run under the store lock and SQLite's file lock; a
  second opener sees the bumped `user_version` and applies nothing.

**Migration-authoring contract.** Phase-2 work that persists a new shape does two things:
(1) **append a `Migration`** in `store/migrations.py` with the next ascending `version`
and bump `CURRENT_SCHEMA_VERSION`; keep the body additive/idempotent (`IF NOT EXISTS`,
additive `ALTER TABLE`) so it is safe on a DB at any older version. (2) If the new shape
changes how a stored record *kind* is interpreted, **register a read-path up-converter**
in `RECORD_UPCONVERTERS` (keyed by `kind`); it lifts an individual legacy row's JSON
envelope to the current shape lazily on read in `get_record` / `list_records` — the
record analogue of CRA-171's `Emission.from_event`. A migration fixes the table; the
up-converter fixes a row, without a bulk rewrite. (Emission retention/rotation and
`max_per_run` are a separate concern, deliberately out of scope here.)

## Agent-language foundations (Milestone F)

Milestone F lays the *substrate primitives* the agent-language operators (Refine, Program,
Quorum, Escalate, the Tuner) build on. The operators themselves are not shipped; what
shipped are the contracts below. All identity additions are forward-compatible and
*fold-only-when-non-default* — see [ADR 0019](decisions/0019-content-hash-version-bump-and-migration.md).

### Output content hash — the canonical content identity

- **`crawfish.output.output_content_sha(o) -> str`** is the single content-hash primitive:
  a lowercase hex SHA-256 over the **canonical JSON** of the structural-equality fields
  (`output_schema`, `value`, `produced_by`, `lineage`, `tainted`). The per-instance `id`
  is **excluded** — it is a fresh UUID, so including it would make every Output hash
  unique; excluding it makes two structurally-equal Outputs hash equal regardless of `id`.
  The `Output` model is unchanged (still frozen, no `sha` field). A `_CONTENT_SHA_VERSION`
  is folded into the payload; bumping it re-keys any ledger persisted on the sha. Every
  consumer that needs "an Output's content identity" (ledger `output_ref`, no-progress-by-sha)
  reads this one function.

### Replay cassette key = the execution coordinate (F-1)

- `runtime/replay.py`'s `_key(request, *, org_id="local", coordinate=None)` is the
  versioned **execution-coordinate / run-identity contract**. Beyond the legacy core
  (`id`, `version`, `role`, `model`, `inputs`, `session_id`) it folds three components
  **only when non-default**: an `ExecutionCoordinate` (frozen dataclass with
  `sample_index` / `iter_index` / `visit_count` / `depth` axes, each `Optional[int]`),
  `org_id` (when `!= "local"`), and `decode_seed`. An all-`None` coordinate folds nothing.
- **Back-compat is pinned:** with no coordinate, `org_id == "local"`, and no decode field,
  `_key` reproduces the exact pre-F-1 key (legacy cassettes still resolve). Every operator
  that re-runs a leaf (quorum, Refine, MCTS, recurse) **must stamp its coordinate axis** so
  each re-run gets a distinct cassette instead of colliding into one.

### Loop / program ledger — a composite key space (F-2)

- The linear pipeline ledger (`checkpoint_step` / `completed_steps`) is unchanged. F-2 adds
  an **explicit extended key space** alongside it under a new `ledger_loop` record kind (no
  new table): loop/back-edge visits keyed `(loop_id, item_id, edge_id, visit) -> output_ref`,
  and a recurse-depth variant `(loop_id, item_id, depth) -> output_ref`. Each iteration pins
  the F-0 `output_content_sha` of its frozen `Output` as the `output_ref`.
- `compute_loop_id(body_version_sha, item_lineage, edge_id)` is **deterministic** — a
  length-prefixed, version-tagged (`_LOOP_ID_VERSION`) SHA, **never** `new_id()` — so two
  process invocations of the same loop over the same item re-derive the same id and resume
  re-charges \$0 for already-recorded iterations. Migration 3 adds an `(org_id, kind)` index
  to keep the completed-iteration scans sargable; the pipeline ledger is untouched
  (see [ADR 0019](decisions/0019-content-hash-version-bump-and-migration.md)).

### AgentRuntime determinism tier + decode-knob ownership (F-5)

- The `AgentRuntime` contract advertises a **determinism capability tier**:
  `DeterminismTier((str, Enum))` = `HONORS_SEED` / `BEST_EFFORT` / `NONE`, with
  `AgentRuntime.determinism_tier` defaulting to `BEST_EFFORT`. This separates model
  stochasticity from infra-nondeterminism: `cw.calibrate` attributes a `BEST_EFFORT`/`NONE`
  backend's residual variance to infra (a variance floor), not to the Definition.
- **Decode-knob ownership** (ADR 0017): the tunable knobs `temperature` / `top_p` /
  `sample_k` live in exactly one place — the `AgentSpec` on the Definition — and enter its
  content hash. `RunRequest.temperature` is a read-only **derived** property, never a
  settable field. Per-call knobs live on `RunRequest`: `grammar` (constrained decode, **not**
  hashed — provider dialect, degrades gracefully) and `decode_seed` (**not** hashed; folded
  into the F-1 cassette key instead). So every decode field enters run identity exactly once.

### Cost model — single owner, one composition law (F-6)

- **`cost.py` is the single owner of the cost model.** No other module re-implements
  estimation or re-defines an operator's cost multiplier. `CostEstimate` gains **additive**
  `expected_usd` / `worst_case_usd` / `expected_lo_usd` / `expected_hi_usd`; the scalar
  `total_usd` is **unchanged** — it stays the lower bound (every cost-bearing operator fires
  once). A bare estimate is the degenerate interval `[total, total, total]`, so existing
  call sites and tests are untouched.
- A `CostShape` describes one cost-bearing operator wrapper and its re-run multiplier;
  `compose_cost(base, shapes)` folds a nesting of shapes (outermost-first) onto a base.
  **The composition law is multiplicative along operator nesting:**
  `worst_case_usd = total_usd × Π shape.worst_case_factor()`. Per-operator worst-case
  factors: `Refine` → `max_iters`, `Escalate` → `1 + strong_price/base_price` (re-priced on
  the strong model), `Quorum` → `k`, `Retry` → `n`, `recurse` → `branching ** max_depth`.
  Worked example: `Refine(4) ∘ Escalate(2×) ∘ Quorum(5)` previews `40×` the lower bound.
- The **expected band** is CI-aware and never falsely precise:
  `expected_factor = 1 + p·(worst_case_factor − 1)` where `p` is the operator's measured
  escalation/retry rate (from `cw.calibrate`/ledger), with `rate_ci` widening the
  `expected_lo`/`expected_hi` edges. With **no** measured rate, `expected == worst_case`
  (never undercount); a model validator enforces
  `total ≤ expected_lo ≤ expected ≤ expected_hi ≤ worst_case`. CL-3 and ALG-5 are
  *consumers* of this API, not editors.

### The gate algebra + statistical substrate (F-3 / F-8)

- **`crawfish.experiment`** is the shared, pure, stdlib-only statistical substrate
  (`paired_bootstrap_ci`, `holm_correction`, `k_from_alpha`, `tune_gate_split`,
  `winners_curse_shrink`, power helpers, `anytime_valid_bound`). No numpy/scipy; bootstraps
  are seeded via a local `random.Random` so identical inputs+seed are byte-for-byte
  reproducible. The normative spec
  [`experiment-design.md`](experiment-design.md) is the **conformance gate** every
  statistical consumer (`calibrate` / `gate` / `quorum` / `explore` / `guard`) must cite.
- **The gate algebra** (`eval.py` / `metrics.py`, single owner) reconciles three gate
  notions and names which consumer uses which — none re-implement stats, all consume
  `crawfish.experiment`:

  | Gate | Function | Consumer |
  |------|----------|----------|
  | relative-regression | `metrics.is_regression` / `eval.gate_against_baseline` (unchanged) | cheap mean-only callers |
  | variance-aware aggregate | `metrics.is_regression_variance_aware` (new; `std=0,k=0` reduces to the above byte-for-byte) | callers retaining a per-metric `std` |
  | variance-aware **paired** | `eval.paired_gate` (new) | the Tuner / `calibrate` / promotion gate |
  | absolute-precision | `eval.precision_gate` (new; **fails closed**) | verifiers / guards / consequential sinks |

  `paired_gate` analyses per-case deltas over identical GoldenSet cases via
  `paired_bootstrap_ci` (CI strictly above 0 ⇒ promote; straddling 0 ⇒ reject), with Holm
  family-wise correction or a primary+guardrails design. `precision_gate` is **absolute**
  and **fails closed** — no baseline ⇒ reject (see [SECURITY.md](SECURITY.md)).

### Store-backed exclusive borrow / train mode (F-7)

- `crawfish/borrow.py` provides a **dynamic exclusive borrow** for switching a `Definition`
  into train/mutate mode, enforced by a Store-backed atomic claim (reusing
  `claim_idempotency`) — never an in-process registry. `mutable(target, store, *, org_id=...)`
  is a context manager that acquires on enter and releases on exit (idempotent, even on
  exception); an **epoch** in a `borrow_lock` record makes the single-shot claim
  re-acquirable. The borrow lifetime is exactly the `with` block and concurrency is rejected
  at acquire. Because enforcement lives in the Store, the guarantee holds across processes and
  survives the SQLite→Postgres swap. See [ADR 0018](decisions/0018-borrow-lifetime-semantics.md).

## Packaging

- `packages/crawfish` — the OSS framework (the `pip install crawfish` distribution).
- `packages/crawfish-cma` — the CMA/ManagedRuntime backend (later).
- Module discovery reads the `crawfish.sources` / `crawfish.sinks` /
  `crawfish.definitions` / `crawfish.types` entry-point groups.
- A user project is **self-contained** (root = the project); `.crawfish/` is
  generated state only; installed plugins live in site-packages, pinned by
  `crawfish.lock`.

## Conventions

- The product model **never imports the SDK** — all model calls go through
  `AgentRuntime`. No raw SQL escapes the `Store` implementation.
- See [`SECURITY.md`](SECURITY.md) for the security spine and
  [`decisions/`](decisions) for ADRs.

!!! note "Good to know"
    The three seams are the load-bearing rule. As long as the product model imports only
    `AgentRuntime`, `Store`, and `ArtifactStore` protocols — never a concrete backend —
    cloud and scale stay a configuration change. Breaking that rule is what turns a swap
    into a rewrite.

## See also

- [Security](SECURITY.md) — the prompt-injection boundary, secrets, and taint
- [Emission taxonomy](emission-taxonomy.md) — the frozen observability contract
- [Experiment design](experiment-design.md) — the statistical-conformance gate for the eval plane
- [API stability](API-STABILITY.md) — semver and deprecation policy
- [ADRs](decisions) — the decisions behind these seams
