# Concepts

Crawfish runs bulk agent work as a typed pipeline you author as directories. This page is the mental model behind the framework; each section maps to real public API and hands off to a reference page for exact signatures.

On this page:

- [The directory model](#the-directory-model) — an agent is a directory
- [The pipeline](#the-pipeline) — `Source → Batch → Aggregator → Router → Sink`
- [Runtimes](#runtimes-the-swappable-agent-loop) — the swappable agent loop
- [The static-vs-fluid boundary](#the-static-vs-fluid-prompt-injection-boundary) — prompt-injection defence
- [Secrets by reference](#secrets-by-reference) and [safe egress](#safe-egress-the-sink-invariants)
- [Team coordination](#team-coordination), [Store seams](#the-store-and-artifactstore-seams), and [cost & inspection](#cost-budgets-and-inspection)
- [The measurement loop](#the-measurement-loop) and [the control plane](#the-control-plane-refine-and-verify) — Refine & Verify
- [The composition surface](#the-composition-surface-branch-cycle-recurse) — branch, cycle, recurse
- [The PyTorch-for-LLMs half](#the-pytorch-for-llms-half-train-eval-and-the-tunable-knob) — train/eval mode, calibration, variance-aware promotion
- [The agents-as-variables half](#the-agents-as-variables-half-compose-version-summon) — compose, version (git for agents), and summon knowledge

## The directory model

An agent is a directory: markdown for instructions and skills, Python for typed I/O, tools, and policies. You write markdown for the instructions and skills, and
Python for the typed inputs and outputs, tools, and policies. The compiler reads the
directory and turns it into a typed `Definition`. Here's what it looks for:

| Path | Becomes |
| --- | --- |
| `instructions.md` | the lead/main agent (front-matter = topology, body = prompt) |
| `agents/*.md` | one subagent each (role = filename stem) |
| `definition.py` | typed `inputs`/`outputs`, `dependencies`, `coordination`, `lead` |
| `tools/*.py` | a tool named after the file stem (a callable of that name) |
| `policies/*.py` | module-level `Policy` instances → `DefinitionAssets.policies` |
| `mcp/*.py` | module-level `MCPConnection` instances |
| `skills/*.md` | skill assets |
| `pyproject.toml` | identity (`name`) + version |

Compile with `Definition.from_package(path)` (or `load_definition(path)`). A Definition's
identity is **content-derived**: a sha over the directory's contents, never its path or a
timestamp. So a directory and its installed package compile to the same thing, byte for
byte. The compiler writes a `definition.lock` for reproducibility. If an agent references
a tool, policy, or delegate that doesn't exist, that broken binding fails at **load
time** — you find out up front, not partway through a run.

A compiled `Definition` is `Freezable`. Call `.freeze()` to seal it into an immutable
artifact; mutating a frozen one raises `FrozenError`.

See the [authoring reference](../reference/authoring.md) and [definition reference](../reference/definition.md) for the full directory contract and `from_package` signature.

## The pipeline

Bulk work is a pipeline of `Node`s. Data fans out into per-item runs, fans back in, branches, then exits through one sink.

```
Source → Filter → Batch(Definition) → Aggregator → Router → Sink
              ├─ fan-out:    one Run per item   (map)
              ├─ Aggregator: N Outputs → one    (reduce)
              └─ Router:      branch by label    (branch)
```

Data flows between stages as an `Output`: a frozen envelope that carries the value, its
schema, and the id of the node that produced it. Nodes never change an Output in place. To
transform one, a node calls `derive` to make a fresh copy, leaving the original intact for
audit. Adjacent stages are **type-checked when you assemble the pipeline** (structural
`parameters_compatible`), so a mistyped wire is caught before any model call.

- **`Source`** is where data enters the pipeline. `fetch()` returns a typed `Output`. A
  *multi* source (`multi = True`) returns a list, and `fan_out` splits that list into one
  `Output` per item — each one seeding its own `Run`. The built-ins are `RepoSource`
  (single) and `PullRequestSource` (multi). Both are deterministic and need no network
  (they read from fixtures).
- **`Filter`** is a pure, synchronous node that narrows a list `Output` by a predicate
  and preserves order. Factories: `title_contains`, `field_equals`, `field_matches`,
  `limit`.
- **`Batch`** is the assembly point. You wire `Source`s and `Output`s into a `Definition`
  with `.add_input(...)`. A multi source fans out to one `Run` per item, and
  `check_wiring()` type-checks at assembly. The batch's cost ceiling carries onto every
  child `Run`.
- **`Aggregator`** is the fan-in counterpart — it does the reverse of fan-out, taking N
  item `Output`s and emitting one. The built-in reducers (`collect`, `concat`, `count`,
  `dedupe`) are pure; a `definition_reducer` runs an agent team to reduce, for example to
  summarize. `fan_in` is the barrier that handles partial success: it drops failed or
  `None` items and supports a `quorum`.
- **`Router`** sends an `Output` down one labelled branch, chosen by a `Classifier`.
  Classifiers come in two flavours: `from_predicates` (pure) and `from_definition`
  (agent-backed). The label set is closed and always includes a `default` (dead-letter)
  label, so every item is routable. Unroutable wiring raises `UnroutableLabelError` at
  **assembly time**.
- **`Sink`** is the only place a pipeline performs an external side effect. The built-ins
  are `LinearSink` and `GitHubPRSink`, both dry-run by default and network-free. Three
  invariants keep egress safe (below).
- **`Workflow`** is the top-level deployable: ordered steps with the `Output` threaded
  from stage to stage, adjacency type-checked at assembly. Orchestration state is
  checkpointed to the `Store` after each stage, so a crash mid-workflow resumes from the
  last completed step.

!!! warning

    `Sink` targets and idempotency keys are **static-only**. A fluid (model-influenced) value can never redirect where a write lands. See [Safe egress](#safe-egress-the-sink-invariants).

For exact node signatures, see the reference pages for [Source & Filter](../reference/nodes-source-filter.md), [Aggregator](../reference/nodes-aggregator.md), [Router & Sink](../reference/nodes-router-sink.md), and [Output & wiring](../reference/output-and-wiring.md).

## Runtimes — the swappable agent loop

Going from dev to prod is a runtime swap, not a code change. `AgentRuntime` is the **only** place the model SDK or CLI is touched. Every run goes
through this one interface, which is what makes the backend swappable:

| Runtime | Backend | Key | Cost | Use |
| --- | --- | --- | --- | --- |
| `MockRuntime` | pure function of the request | no | $0 | dev loop, tests, benchmarks |
| `CommandRuntime` | local `claude -p` subprocess | no | your Claude session | real local runs |
| `RecordReplayRuntime` | wraps any runtime; cassettes | no on replay | $0 on replay | snapshot/replay tests |
| `ClientRuntime` | API client | yes | metered | (stub today) |
| `ManagedRuntime` | hosted CMA | yes | metered | (stub today) |

`get_runtime(name)` resolves a runtime by name from `RUNTIME_FACTORIES`.

- **Dev loop.** `MockRuntime` returns deterministic canned text with no model call.
  Iterating on a Definition or a metric never burns budget, and scores never drift.
- **Replay.** `RecordReplayRuntime(inner, cassette_dir, record=True)` records real
  `RunResult`s once, then replays them at zero cost. If a cassette is missing and
  recording is off, it raises `CassetteMiss`. This is what makes `craw dev` and
  `craw test` fast and deterministic.

See the [runtimes reference](../reference/runtimes.md) for the `AgentRuntime` interface and each backend's exact behaviour.

## The static-vs-fluid prompt-injection boundary

Untrusted data reaches the model as data, never as instructions — this is the rule that stops it from hijacking your agents. Every `Parameter`
carries a `Flow` that marks it trusted or not:

- `Flow.STATIC` is **trusted config**, set once per batch — a repo, a project. It can go
  straight into the agent's instructions.
- `Flow.FLUID` (the default) is **untrusted per-item data**, like a ticket body. It goes
  *only* inside a clearly marked, labelled data block, and the instructions tell the model
  to treat that block as data, never as instructions. Trusted config and untrusted data
  never mix.

The prompt compiler enforces this. `split_inputs` sorts inputs by their declared flow,
and anything unknown defaults to fluid — the safe, untrusted side. This is the
load-bearing defence against prompt injection. A ticket body can't smuggle instructions
into the agent. And because sink targets must be static, a value the model influenced
can't redirect where a write lands.

!!! warning

    `Flow.FLUID` is the default, and any input you don't classify is treated as **untrusted**. Mark a parameter `Flow.STATIC` only when it is trusted config set once per batch — never to admit per-item data into the instructions.

See the [type-system reference](../reference/type-system.md) for `Flow`, `Parameter`, and `split_inputs`.

## Secrets by reference

Credentials never reach a prompt — Crawfish holds them by reference only. Config stores the *name* of an environment
variable, like `"GITHUB_TOKEN"`, never the value itself. The value is resolved at the
egress boundary by `resolve_secret` and injected into a tool's or MCP server's
environment. It never reaches a prompt, the stored config, an `Output`, logs, or
telemetry. An `MCPConnection`'s `auth` field is a secret reference by construction.

!!! warning

    A secret value never enters a prompt, an `Output`, the stored config, logs, or telemetry. Pass the **name** of the env var (`"GITHUB_TOKEN"`), and let `resolve_secret` inject the value at the egress boundary.

See the [secret-broker reference](../reference/secret-broker.md) and [secrets & consent reference](../reference/secrets-and-consent.md) for `resolve_secret` and the consent model.

## Safe egress — the Sink invariants

A `Sink` is the one place a pipeline performs an external side effect. Three invariants keep that safe:

1. **Static-only targets.** Destination slots — repo, project, channel — must be
   `Flow.STATIC`. A fluid target is rejected at construction with `TargetMustBeStaticError`,
   so a prompt can never redirect a write.
2. **Idempotency.** Every write is keyed by a hash of *static config only* plus the batch
   and output identity, never the fluid or model-derived value. Re-running the same batch
   is a no-op, not a duplicate side effect.
3. **Approval gate.** An `always_ask` sink refuses to fire without an explicit approval
   callback, raising `ApprovalRequired`. A run can also suspend durably on approval before
   spending any compute (`requires_approval` → `RunSuspended`).

See the [Router & Sink reference](../reference/nodes-router-sink.md) for the sink built-ins and these invariants in full.

## Team coordination

A `TeamSpec` carries the multi-agent topology — agents delegate in and return a typed result out, rather than sharing a message bus. Coordination leans on Claude's
**hierarchical subagent model** rather than a bespoke message bus:

- **`SINGLE`** is one agent, or several independent agents, with no coordinator.
- **`LEAD`** is a lead that dispatches the roles in its `delegates_to`, then combines
  their typed results. Each subagent result re-enters the lead as **fluid data**
  (`{role}_result`), never as instructions, which preserves both typing and the injection
  boundary.
- **`SEQUENTIAL`** runs agents in declared order; each result threads into the next as
  `prior_result`.

`run_team` executes the topology and returns one `RunResult`. The coordinator is
runtime-agnostic — it works with the mock, so tests stay deterministic.

!!! note "Good to know"

    Each subagent result re-enters the lead as **fluid data** (`{role}_result`), never as instructions. The injection boundary holds inside a team, not just at its edge.

## The Store and ArtifactStore seams

All persistence goes through the `Store` protocol — swap the backend without touching the code that uses it. A `Store` is a *seam*, meaning a clean interface
you can swap the backend behind without touching the code that uses it. The product model
imports the *protocol*, never a concrete backend, so moving from SQLite to Postgres is a
driver swap, and no raw SQL appears at any call site. Every row carries an `org_id`
tenancy key (defaulted to `"local"`), so cloud multi-tenancy is also a driver swap, not a
schema migration. The local default is `SqliteStore`. The `Store` backs typed records, KV
and working memory, idempotency claims, and the append-only event ledger that powers the
inspector.

- **`Memory`** is a thin `Store`-backed KV and dedup handle, scoped to a
  `(namespace, org_id)` pair. It covers working memory (`get`/`set`), cross-run dedup
  (`already_processed`/`mark_processed`), and an atomic `claim` that wins exactly once per
  id. Because state lives in the `Store`, dedup survives across runs.
- **`ArtifactStore`** (with `LocalArtifactStore` and `offload_if_large`) is the blob seam:
  the local filesystem now, S3 later. Large payloads are offloaded by reference instead of
  carried inline.

See the [persistence reference](../reference/persistence.md) for the `Store`, `Memory`, and `ArtifactStore` contracts.

## Cost, budgets, and inspection

You see the bill before a single model call, and you reconstruct any run from the event ledger.

- **`estimate_cost(definition, items=N)`** is a deterministic dry-run preview. It assumes
  one run per agent per item, prices it from a coarse per-model table, and returns a
  `CostEstimate` — so you see the bill before a single model call. The `mock` model is
  free, so dev and replay preview at $0.
- **`CostBudget`** is the hard ceiling the orchestrator can kill a run on, raising
  `BudgetExceeded`. **`Budget`** layers a warn/stop policy on top (`BudgetState` is
  ok/warn/stopped), and **`CostMeter`** is a live accumulator that tracks remaining
  headroom.
- **`inspect_run(store, run_id)`** derives a `RunReport` — status, cost, latency, tool
  calls, transcript — purely from the Store's append-only event ledger, with no live model
  call. `tail_events` is the poll primitive for live streaming, and `format_report`
  renders a report for the CLI. This is the trust and devtools layer that backs
  `craw inspect` and `craw logs`.

See the [context & budgets reference](../reference/context-and-budgets.md) and the [inspector reference](../reference/emission-inspector-visualize.md) for the exact budget and report types.

## The measurement loop

`Metric` → `Rubric` → `Benchmark` make quality measurable and comparable across Definition
versions. The eval data lifecycle (`EvalCase`, `GoldenSet`, `LLMJudge`, `capture_case`,
`grade_output`, and `save_baseline`/`load_baseline`/`gate_against_baseline`) lets you
capture real runs as reusable cases, curate versioned golden sets, grade with an
LLM-as-judge, and gate a candidate against a stored regression baseline. All of it is
deterministic under mock and replay.

See the [evals reference](../reference/evals.md) and [metrics reference](../reference/metrics.md) for the full lifecycle.

## The control plane — Refine and Verify

One primitive in Crawfish is stochastic: a model `Run`. Everything else is
deterministic, typed, versioned, and taint-tracked. The **control plane** is what wraps
that single stochastic primitive in a *loop* without giving up any of those properties —
"keep trying until good enough, but never past N tries or $X, and resume a crash for
free." `Refine` is that loop; `Verifier` is the critic that can stop it.

**`Refine` generalises the bounded/metered/durable loop.** The framework already had
three fixed-bound re-run atoms — `EscalatingRuntime` (2×), `Run._repair` (+1),
`RetryPolicy` (on-exception). `Refine` folds them into one goal-driven operator: run a
producing body `Definition`, check each frozen `Output` against an **external**
`StopCondition`, and iterate until satisfied or a bound is hit (`max_iters`, the shared
`CostBudget`, cooperative cancel, or noise-aware no-progress — **never wall-clock**). It
mutates nothing: every attempt is a fresh frozen `Output`, the body stays frozen. That is
`mutable = False` — **eval mode**.

**The stop signal must be external — and a critic must *earn* the authority to stop you.**
A bare `Verifier` only describes an `Output` (a closed label set with a mandatory
`default`); it is in `WARN`/`SHADOW` and **cannot** stop a loop. `Verifier.gated(...)` is
the only path to a `GatedVerifier` with `BLOCK` authority, and it **fails closed**: a
never-benchmarked critic, or one below `min_precision` against a decision `GoldenSet`,
raises `VerifierNotGated` rather than being trusted. This is the same discipline as a
`Sink`: stopping a loop ships the result, so the authority to stop is conferred by a gate,
not asserted. And a `Refine` whose verifier critic shares the body's `content_sha()` is
rejected outright — the generator may never critique itself.

**$0 crash-resume falls out of the spine, not a special case.** With an `ExecutionLedger`,
each completed iteration's frozen `Output` is checkpointed under a **deterministic** loop
id. On `resume=True` the committed iterations replay through the cassette runtime at zero
cost, and because each iteration's `produced_by` is the deterministic
`body.content_sha()#visit` coordinate, the replayed Output's content sha reproduces the
checkpoint **bit-for-bit** — determinism is *verified*, not trusted. A loop that died at
iteration 3 of 5 restarts at iteration 4 charging `$0`.

See the [Refine & Verify guide](refine-and-verify.md) for the runnable walkthrough.

## The composition surface — branch, cycle, recurse

`Refine` is the control plane for *one* body. The **composition surface** gives the agent
language its *shape*: control flow that branches, cycles, and recurses — while keeping
every property the rest of the framework holds. Control flow here is **deterministic,
versioned, and taint-tracked**, cycles are **bounded and crash-resumable at \$0**, and
recursion re-enters only **frozen** Definitions. It is the structural keystone the Tuner
and the rest of Phase 2 compose onto.

**A `Router` becomes a runnable step.** `branch(classifier, branches)` dispatches each
item through the *same* step machinery as its chosen branch, so a branch may be a
`Sink`/`Batch`/`Filter`/`Aggregator` and inherits the identical budget / taint /
checkpoint guarantees. The label set is closed and totality-checked at construction, and
`check_types` verifies every branch accepts the upstream output — a mistyped or uncovered
branch fails at **assembly**, before any model call.

**A `Program` is a typed graph whose edges may cycle.** It reuses the `Workflow` kernel;
the difference is the *driver* — it walks edges per item rather than running the steps
once. A back-edge re-enters its region while a guard predicate holds, and every traversal
is a **content-addressed version transition** (`Output.derive` mints a fresh sha; the
frozen Output rejects in-place edits — this is eval mode). Cycles are bounded by
iteration / shared budget / cancel / calibrated no-progress — **never wall-clock** — and a
back-edge with no `max_visits` is rejected with `UnboundedCycleError` at assembly. One
shared `CostBudget` meters every iteration, and taint carries across every edge.

**Durable \$0 resume falls out of the same ledger, not a special case.** Each iteration is
checkpointed under a **deterministic** loop id over the F-2 composite-key ledger; on
`resume=True` the committed iterations replay through the cassette runtime at zero cost,
and because each iteration's `produced_by` is the deterministic
`{region_version}#{edge_id}#{visit}` coordinate, the replayed Output's content sha
reproduces the checkpoint **bit-for-bit** — determinism is *verified*, not trusted. Every
ledger row carries `org_id`, so a cross-tenant resume is isolated.

**`recurse` re-enters a frozen Definition under a depth bound.** Recursion is a
depth-guarded `Program` back-edge into the *same* frozen body, pushing a frozen version
onto a per-item depth stack and folding the descent-order children with an existing
reducer. `max_depth` is mandatory (`UnboundedRecursionError` otherwise), the whole-tree
shared budget guards the `O(b^d)` fan-out, and a fold **never launders taint** — the
reduced Output is tainted if any child input was.

See the [Compose guide](compose.md) for the runnable walkthrough.

## The PyTorch-for-LLMs half — train, eval and the tunable knob

Everything above is the *deterministic, typed, versioned* half of Crawfish: an agent is a
frozen artifact with a content hash, and the same inputs reproduce the same outputs. This
section is the **other** half — the part that *learns*. The thesis is one sentence: **an
agent is a model with tunable weights, and `mutable` is the train/eval switch.** The two
halves are not bolted together; they are unified by mutability itself. A frozen Definition
is in **eval mode** (reproducible, the only mode that may act); an unfrozen copy is in
**train mode** (its knobs may move). PyTorch's `requires_grad` and `.eval()` are the direct
ancestors, and the same hard lesson applies: *which knobs may move* and *whether the
artifact is sealed* are **orthogonal axes**.

**Axis 1 — `tunable` is data, not a flag on the model.** Which knobs the Tuner may search is
a `TuneSpec`: a content-hashable list of `KnobDomain`s, each a dotted `path` into the knob
vocabulary (`agent.<role>.model`, `.prompt`, `.temperature`, `.policies`, `team.coordination`,
`injected_prompts`) plus the discrete `values` it may take and a `tunable` bit. A pinned
knob (`tunable=False`) is never proposed. The spec is authored as `tune.toml` and folds into
the Definition's content hash via `tune_spec_sha` — so **changing the search space versions
the agent**, exactly like editing any other knob. Tuning *is* a content change. (An empty
`tune.toml` is hash-neutral: a tune-less Definition keeps its sha byte-for-byte.)

**Axis 2 — `train()` / `eval()` is the mutability switch.** `train(defn)` returns an
*unfrozen* deep copy with a fresh `Version`; a training mutation is copy-on-write — it mints
a new `version.sha` only when re-frozen, never an in-place edit of the original.
`eval(defn)` re-freezes via the content-hash path, so `eval(train(d))` is **idempotent**:
it hashes back to `d`'s eval sha whenever no knob actually moved. The load-bearing rule sits
on this axis: `guard_consequential(defn)` raises unless `defn` is eval-mode. **A
consequential side effect — a Sink write, a recorded run — is eval-only**, because a training
artifact has no stable content identity to key idempotency or attribute the effect to. The
prompt-injection boundary and the train/eval boundary are the *same* boundary: only sealed,
content-addressed, eval-mode agents touch the world.

**Calibration is the noise band.** A single benchmark run hides run-to-run variance, so the
old eval gate compared two point estimates and the escalation threshold was a guessed
constant. `calibrate(...)` runs each golden case `runs` times under distinct,
deterministically-derived seeds and returns a `CalibrationReport`: per-metric `rubric_mean`
and `rubric_std` (the **noise band**), `output_variance` (structural disagreement across
re-runs), and — when cases carry labels — Brier (primary), ECE with a bootstrap CI
(diagnostic), a reliability curve, and an **evidence-derived** `abstention_threshold` read
off that curve rather than chosen. It refuses a replay runtime (replay would fabricate
zero variance) and is bounded by the same autonomy ceiling as the Tuner.

**Promotion is variance-aware.** `promote_against_baseline(...)` promotes a candidate only
when its gain over the stored baseline **clears the per-metric noise band** (`k·std`,
`k` from `alpha`), and only when *no* metric regresses past its own band — the hard F-3
rejection invariant is preserved, just made noise-robust. A candidate that maxes one metric
while truly regressing another is still rejected; a within-noise "win" does not promote. With
no recorded `std` the band is zero-width and the gate reduces byte-for-byte to the
single-point behaviour, so every pre-existing baseline keeps working.

**The objective is cost-regularized.** A pure-quality rule would promote a 1%-better,
5×-pricier candidate. An `Objective` re-ranks — only **among candidates that already pass the
hard regression gate** — by `Σ wᵢ·scoreᵢ − λ·cost − μ·ece`, with the cost term normalized so
`λ` is unit-free. Cost can break a tie or veto a marginal gain, but it can **never** promote
a quality regression. An ε-constraint form minimizes cost subject to a quality floor.

**The weights transfer.** `state_dict(defn)` extracts the tunable knobs — per-role
`RoleKnobs`, the coordination choice, `injected_prompts`, and summoned units as
**references-by-version** — as a JSON-only `StateDict`, carrying *no* architecture and *no*
executable nested Definition. Its `structure_sha` is the architecture identity (the
transfer-compatibility key); its `sha` is the knob-value identity. `load_state(defn, state)`
is copy-on-write: `strict=True` refuses a shape mismatch (`IncompatibleStateError`),
`strict=False` loads the structural intersection, and `only=[...]` transfers just the named
knob groups. This is the architecture/weights split — Hugging-Face-for-agent-weights: learn
on one Definition, carry what it learned onto a sibling of the same shape.

**Serving has an explore dial.** `ServingLoop` is the serving-time explore/exploit overlay:
it routes `(1-ε)` of live items to the promoted best and `ε` to a trial candidate, choosing
*which* items explore by a seeded hash of the recorded `item_id` — so a replay re-explores
**exactly** the same items. ε follows a decaying schedule and is bounded by the shared
`CostBudget`. The trial `graduate`s only after a **pre-registered sample size** (no peeking —
continuous optional-stopping would inflate false promotions), and even then only through the
eval gate on the `LearningLoop`. Both arms are frozen, eval-mode Definitions; **only static
knobs are ever promoted**, so the learning loop stays inside the
[security spine](../architecture/SECURITY.md).

Learn it end to end: the [Train, calibrate & promote guide](train-and-tune.md) (runnable,
mirrors the triage demo) and the [Tuner & learning reference](../reference/tuner-and-learning.md).

## Taming the stochastic primitive — vote, decline, distil, constrain

The control plane wraps the one stochastic primitive — a model `Run` — in a loop; the
composition surface gives that loop shape; the tunable-ML half searches its knobs. The
**tameness layer** is the fourth move: it bounds the stochastic leaf *itself*, with four
disciplines that compose onto any producing step while keeping every determinism, typing,
and taint guarantee intact. Each does one thing to the leaf — vote it down, let it decline,
distil its invariants, constrain its surface.

**Quorum votes the variance down.** Self-consistency — sample `N`, take the consensus — is
the cheapest, best-attested variance reducer and the purest expression of the thesis: **N
stochastic leaves reduced by a deterministic vote**. `QuorumRuntime` wraps any inner
runtime, samples the same request `k` times (each a distinct seeded leaf charging the shared
budget, replayable under its own cassette via the execution coordinate), and reduces by a
typed, **pure** consensus vote. `majority_vote` is the modal-output estimand with mandatory
canonicalization (`{"a":1,"b":2}` ≡ `{"b":2,"a":1}`); on an ill-defined plurality it
abstains to a *declared* default, never a silent pick. `k` defaults to the tunable `sample_k`
knob, and a sequential proportion test stops early once a Wilson lower bound on the leader's
share clears `0.5` — no peeking penalty. A vote **never launders taint**: the winner is
tainted iff any sample was (ALG-7).

**Abstention lets a step decline instead of hallucinate.** Selective prediction is the
formal frame for a reliable agent, and the tameness layer could escalate but never *give
up*. `abstain_below(threshold)` measures the run's self-reported confidence (a fluid
self-report is **data**, never an instruction) and either passes a confident Output through
unchanged or returns a fresh Output carrying a typed `Abstention`. The abstention is a
**value**, tagged `_abstention` *in the JSON* (no Python type survives a replayed Output),
so `is_abstention` is a pure routable predicate a `Router` branches to review. It is
fail-safe (a missing confidence declines), idempotent, and threshold-calibrated:
`abstain_below_calibrated` reads the confidence off the `calibrate` reliability curve rather
than guessing a constant. Taint propagates into the `Abstention`, so a declined fluid output
can never become a Sink target.

**The house-guard distils a learned rule into a pure invariant.** This is the deepest
expression of the thesis: a program *accretes its own deterministic invariants*. Quality is
**learned stochastically** (`propose_rule` emits a FLUID candidate from one model `Run`),
**distilled** to a pure predicate (`distill` parses it *as data* into a closed grammar —
`Comparison | SetMembership | NumericBound | BoolCombination | Always` — evaluated by an
interpreter that never uses `eval`/`exec`; the proposal can only *select within* the grammar,
never widen it), and only **earns** enforcement after a **joint** precision-and-coverage gate
(`HouseGuard.synthesize`): a Wilson precision *lower* bound clears `precision_floor` **and**
coverage clears `min_coverage` **and** the corpus is non-empty, so a 99%-precision /
2%-coverage rule cannot block. It fails closed (no corpus ⇒ stays in `warn`), runs a
`shadow → warn → block` lifecycle, and is content-hashed and reversible. This is the same
earn-the-right-to-gate discipline as the `Verifier`, applied to a *learned* rule.

**Constrained decoding makes a malformed shape impossible.** Decode-time constraint is
*strictly stronger* than the post-hoc validate-and-repair loop: instead of detecting a
malformed output and paying a metered repair re-prompt, the runtime is told the output
**shape** up front, so a malformed value is an *impossible* state, not a repaired one. A
`Grammar` (enum / regex / json_object, derivable from a Definition's declared output schema)
is a frozen, declarative constraint on one field; `enforce` is a **pure** projection onto the
constraint surface, raising only when no candidate exists at all — never a silent coercion. A
grammar-honouring constrained `Run` keeps `repair_count` at `0`. The grammar is **static /
trusted** — it has no constructor that reads a fluid value — and rides on the *per-call*
request, kept out of the Definition content hash (it constrains the decode surface, it does
not version the agent), so the prompt-injection boundary holds: the constraint is config, not
session data.

The house-guard is the keystone — **learn stochastically → distil to a pure predicate → earn
enforcement** — and it is the same shape as everything else in the language: the stochastic
part stays contained, and the program keeps accreting determinism around it. Learn it end to
end in the [Taming stochasticity guide](tameness.md) (runnable, mirrors the triage demo).

## The operator surface — drive, price, and pin the language

The control plane, composition surface, tunable-ML half, and tameness layer are *libraries*.
The **operator surface** makes them drivable from the shell and adds the two honesty
primitives a self-optimizing app needs to trust what it drives.

**The optimization plane is reachable from `craw`.** Five subcommands — `craw eval` (score and
gate on a baseline), `tune` (search the knobs), `refine` (iterate to a goal), `learn`
(self-version, or roll back), and `guard` (distil a deterministic guard) — bind the
already-shipped primitives without re-implementing one of them. The point is closing the loop:
the [self-optimizing app](../roadmap/README.md) drives Crawfish through the same shell you do,
so the optimization plane must be reachable from `craw`. Every command is deterministic by
default (the mock runtime, all randomness through `--seed`), fires no Sink (the plane is
**egress-free**), and emits a versioned `--json` schema a downstream tool parses stably.

**The advertised cost band is a true upper bound.** A point cost estimate is dishonest: one
run per agent is blind to the re-run multipliers escalation, repair, retry, and `Refine` add,
so the number could only *undershoot*. The cost preview is now an honest **interval** — a
`lower` bound, an `expected` band from measured re-run rates, and a `worst_case` that folds
every multiplier *multiplicatively* along the operator nesting (a `Quorum(5)` over an
`Escalating(2×)` previews `10×`, escalation re-priced on the strong model). The contract is
one-directional and load-bearing: **a real run never exceeds `worst_case`**, so a budget set to
it can't be blown by the run it previewed. The fold is pure static analysis — no model call.

**One charge for N identical in-flight calls.** A disk cassette only helps the *second* run;
two identical items in the *same* batch both miss and both spend. **Single-flight** coalesces
them: when N concurrent callers issue the same request, only the leader runs the real metered
call and the rest await its result — exactly **one `inner.run` ⇒ one `CostBudget.charge`**, a
strict strengthening of the gas meter. It is a strict refinement (the coalescing key is the
replay cassette key, so replay is bit-for-bit either way) and tenant-safe (the key is salted
with `org_id`, so org A's computation is never served to org B).

**A reproducible dependency closure.** A Definition *summons* units by reference at a version
constraint; an unpinned transitive closure breaks replay reproducibility. The resolver walks
the closure, picks the highest compatible version, detects conflicts and cycles, and pins
every ref to an exact `(version, sha256)` in a committable lockfile. A run embeds one small
`closure_sha`, keeping run identity small. Reading a lockfile is **data-only** — it never
executes unit code and re-verifies the recorded sha, **failing closed** on a tampered file or a
drift; a mutated unit gets a new sha, so an un-versioned mutation cannot enter a frozen closure
without a re-freeze. Learn it in the
[Drive the language from the CLI guide](optimize-from-the-cli.md).

## The agents-as-variables half — compose, version, summon

The tunable-ML half makes an agent a *model with tunable weights*. This half makes it a
**variable**: a content-addressed value you compose from parts, name, and move through a
version log — **git for agents** — plus knowledge you **summon** by reference as data.

**Composition is copy-on-write.** The `with_*` operators (`with_skill`, `with_agent`,
`with_context`, `with_inputs`, `with_policy`) each take a base Definition, deep-copy it
unfrozen, apply one structural edit, and re-seal it through the **single content-hash path**
— returning a **new frozen** Definition while the receiver is untouched. Two structurally
identical compositions collapse to one sha (idempotent); any knob diff diverges it. Because
every op re-seals, un-versioned mutation is impossible: `with_*` on a frozen receiver copies
first, but mutating the **returned** frozen object raises `FrozenError`. A skill or summon
enters by **reference, not embed** — a version pin folded into `dependencies` — so
`export().checksum` changes *iff* the pinned version changes.

**A name is a mutable pointer over an immutable object store.** That is git's exact
ergonomic, and Crawfish already had the immutable side — a frozen `Definition` is
content-addressed by its `content_sha`. `DefinitionStore` adds the name registry:
`save(name, defn)` stores the body content-addressed (dedup), moves the `name → sha` pointer
(the **sole** mutation), and appends a `DefinitionVersion` lineage event with the `parent`
edge; `recall(name)` (or `recall(name, sha=...)` for a pinned historical version) is
**pure** — it re-seals a stored object and **never mints a new sha**. `save` requires a
frozen (eval-mode) Definition (`UnfrozenDefinitionError` otherwise), and every plane is
`org_id`-scoped. `modify(store, name, fn)` is the commit verb (`recall → fn → save(parent)`,
where `fn` composes via `with_*`); `reset(store, name, to)` is the checkout verb — a **pure
pointer move** that mints no object and refuses an unreachable sha (`UnreachableShaError`).

**Knowledge is summoned by reference, and reaches the model as data.** A `Wiki` is a
versioned, content-hashed, summonable knowledge unit whose `content_sha` is a **Merkle over
page leaves** (a re-hash re-derives only the changed page). `with_page` is copy-on-write;
pages are **tainted by default** and carry a `TrustTier` (`TRUSTED`/`COMMUNITY`/`UNTRUSTED`)
that only ever *raises* suspicion — it never lowers taint. `readonly()` pins it into a
Definition by a `SummonRef` carrying the **content sha, never the body**, so the export
checksum tracks the pin and a secret body can't leak through the reference surface;
`mutable()` is the train-mode edit handle, **rejected on a frozen (eval-mode) Wiki**.
`consult()` materialises a `Context` whose entries are **tainted (fluid)** — so summoned
knowledge flows through the fluid-data block and can never reach an instruction slot or a
static-only Sink. The retrieval half (`Rag`) ships as a **seam only** today
(`RagSeam` / `RagDeferred`); its safety properties — scrubbed embeddings, tainted hits
carrying the source trust tier — are locked in now so the deferred impl can't regress them.

The boundary is the same as everywhere else in the language: only sealed, content-addressed,
eval-mode values touch the world, and summoned knowledge is data, not instructions. Learn it
end to end in the [Agents as variables guide](variables-and-knowledge.md) (runnable, mirrors
the triage demo — compose a variant, save/recall it by name, modify/reset across the version
log, summon a Wiki).

## Next steps

- [Cookbook](cookbook.md) — copy-paste recipes, including eval-as-test.
- [Getting started](getting-started.md) — build and run your first agent.
- [API reference](api-reference.md) — every public symbol.
- [Reference index](../reference/index.md) — the deep per-topic pages.
