# Roadmap

Where Crawfish is and where it's going. This is the public, outside-in view; it
describes capabilities, not internal tracker IDs. For claimable work and discussion,
see the project's GitHub **Issues** and **Discussions**.

<!-- TODO(maintainer): link the real Issues/Discussions URLs once the repo slug is set
     (e.g. https://github.com/Neal-Kotval/crawfish/issues). -->

## Phase 1 — the local trust loop (shipped)

Crawfish runs an end-to-end pipeline locally with **no hosted dependency and no API
key** (a mock runtime drives the demo). The loop:

> A multi-item **Source** fans out → a **Definition** team runs per item via `claude -p`
> → an **Aggregator** reduces → a **Router** branches → a **Sink** writes.

…and it's **typed, versioned, and benchmarked**, with retries, a dead-letter queue, and
crash-resume. What's in the box today:

- **Authoring as directories** — a directory compiles to a typed Definition; single- and
  multi-agent teams; MCP tool access; pluggable context-window management.
- **Typed structural IO + versioning** — structural type compatibility (never string
  equality), freezable/versioned artifacts and lockfiles.
- **Three swappable seams** — `AgentRuntime`, `Store` (WAL SQLite: tenancy, idempotency,
  event ledger), `ArtifactStore`. The product model imports protocols, never backends,
  so cloud + scale stay a driver swap, not a rewrite.
- **The full node set** — Source, Sink (idempotency, approval gate, static targets),
  Filter/Router/Classifier, Aggregator, Memory, durable Run.
- **Pipelines** — fan-out/fan-in Batch, rule-based scheduling, execution-state ledger,
  retries + dead-letter + replay.
- **Measurement & knowledge** — metrics, rubrics, benchmarks against golden sets, eval
  data lifecycle, cost preview + budgets, a streaming run inspector.
- **Operate, observe & integrate** — `craw deploy` (always-on detached supervisor,
  auto-restart, ledger resume), an observer primitive (rules + LLM judge), `craw manage`,
  a loopback-only `craw visualize` dashboard, `craw export --claude-code`, and a
  configurable project structure with `craw doctor`.
- **Security spine** — fluid (untrusted) inputs reach the model as data, never
  instructions; consequential Sink targets and idempotency keys are static-only; secrets
  resolve by reference and are never logged or in-prompt.
- **Ship surface** — `pip install` → `craw init` → a 5-minute zero-key demo;
  `craw build` → container; a MkDocs docs site; an API-stability contract (stable /
  experimental / deprecated tiers + semver).

## The agent language — control plane, composition surface, tunable-ML library, tameness layer, operator surface + variables-and-knowledge shipped (in progress)

Phase 2 includes a larger bet: an **agent language** where composition operators
(Refine, Program, Quorum, Escalate) and a Tuner make agents self-improving over your
data. The first six milestones — the **control plane**, the **composition surface**, the
flagship **tunable-ML library**, the **tameness layer** that bounds the one stochastic
primitive, the **operator surface** that makes the whole optimization plane drivable from
the shell, and the **variables-and-knowledge** layer that makes an agent a content-addressed,
composable, named **variable** (git for agents) with knowledge **summoned** by reference —
have now shipped on top of the foundational primitives:

- **`Refine` — a bounded, metered, durable iterate-until-goal loop.** Run a producing
  Definition, check each frozen Output against an *external* stop condition, and iterate
  until good enough — but never past `max_iters` or a `$X` `CostBudget` (and never on
  wall-clock). It mutates nothing, and with a ledger a crash mid-loop resumes for **\$0**,
  content-hash verified. Folds the three fixed-bound re-run atoms into one operator.
- **`Verifier` — a critic that must *earn* the right to stop you.** A bare `Verifier`
  only describes an Output and cannot gate; `Verifier.gated(...)` admits a `GatedVerifier`
  only after the critic clears an absolute-precision bar against a decision golden set,
  and **fails closed** — a never-benchmarked critic is never trusted to block production.
  A generator may never critique itself.
- **`branch` / `Program` / `recurse` — control flow with shape.** `branch(...)` makes a
  `Router` a runnable step (each branch inherits the same budget/taint/checkpoint
  guarantees). `Program` is a `Workflow` whose **edges may cycle** — back-edges re-enter
  a region while a guard holds, bounded by `max_visits` / budget / cancel / no-progress
  (never wall-clock), and a crash mid-cycle resumes for **\$0**, content-hash verified.
  `recurse(...)` is a depth-guarded back-edge into the *same frozen Definition*. Cycles
  and recursion are **assembly-required to be bounded** (`UnboundedCycleError` /
  `UnboundedRecursionError`); taint carries across every edge, and a fold never launders
  it.
- **The tunable-ML library — an agent is a model with tunable weights (flagship).** This is
  the *PyTorch-for-LLMs* half, unified with the rest by one idea: `mutable` is the train/eval
  switch. `train()`/`eval()` make *which knobs may move* and *whether the artifact is sealed*
  orthogonal axes, and `guard_consequential()` makes **acting eval-only** — only a sealed,
  content-addressed agent touches the world. The tunable knob space is *data* (`TuneSpec`,
  authored as `tune.toml`) that folds into the content hash, so **tuning versions the agent**.
  `calibrate()` measures the run-to-run **noise band**; `promote_against_baseline()` promotes
  only when a gain **clears that band** (the F-3 rejection invariant, made noise-robust); a
  cost-regularized `Objective` re-ranks the gate-passing set so cost can never promote a
  regression. `state_dict()`/`load_state()` are the architecture/weights split
  (*Hugging-Face-for-agent-weights*), and `ServingLoop` is the budget-bounded, no-peeking,
  deterministic-under-replay explore dial. **Only static knobs are ever promoted** — the whole
  loop stays inside the security spine.
- **The tameness layer — bounding the one stochastic primitive.** A model `Run` is the only
  stochastic atom; this milestone bounds it *itself*, four ways, without touching the
  deterministic spine. **`QuorumRuntime`** is self-consistency as a typed operator — sample the
  same request `k` times (each a seeded, replayable leaf charging the shared budget) and reduce
  by a **pure** consensus vote (`majority_vote`, the modal-output estimand); an ill-defined
  plurality abstains to a *declared* default, `k` defaults to the tunable `sample_k` knob, a
  sequential proportion test stops early with no peeking, and a vote **never launders taint**.
  **Abstention** (`abstain_below` / `abstain_below_calibrated`) lets a step *decline* rather
  than hallucinate, as a typed, routable Output **value** (`is_abstention`) with its threshold
  read off the calibration reliability curve. **The house-guard** (`HouseGuard`) accretes the
  program's own invariants — quality is **learned stochastically**, **distilled** to a pure
  closed-grammar predicate (no `eval`/`exec`, the proposal can only select within the grammar),
  and only **earns** enforcement after a **joint** precision-and-coverage gate that fails closed.
  **Constrained decoding** (`Grammar`) makes a malformed output shape an *impossible* state
  rather than a repaired one — a per-call, static/trusted property that keeps `repair_count` at
  0 and stays out of the agent's content hash. The house-guard is the keystone:
  *learn stochastically → distil to a pure predicate → earn enforcement.*
- **The operator surface — the optimization plane, drivable from the shell.** The flagship slice
  completes: the libraries above become drivable from `craw`, and two honesty primitives are
  added. **Five subcommands** — `craw eval` (score + gate on a baseline; exits non-zero on a
  regression), `tune` (search the knobs, cost-regularized, byte-identical under `--seed`),
  `refine` (iterate to a Rubric goal), `learn` (self-version, or `--rollback` with *no* model
  call), `guard` (distil a closed-grammar predicate into a `HouseGuard` at its *earned* stage) —
  bind the already-shipped primitives, are deterministic by default, fire no Sink, and emit a
  versioned `--json` schema. **The honest cost interval** turns the preview into a band
  (`lower` / `expected` / `worst_case`) folded *multiplicatively* along the operator nesting, so
  the **advertised `worst_case` is a true upper bound** a real run never exceeds. **Single-flight
  caching** coalesces N concurrent identical calls onto one metered `inner.run` — exactly one
  `CostBudget.charge` — replay-identical and tenant-safe. **A dependency resolver + lockfile**
  pins a Definition's summoned transitive closure to exact `(version, sha256)`; reading a
  lockfile is data-only and **fails closed** on drift or tampering (`craw lock --check` is the
  CI gate), so an un-versioned mutation cannot enter a frozen closure.
- **Variables & knowledge — an agent is a content-addressed, composable, named variable.**
  This is *git for agents*. **Copy-on-write composition** (`with_skill` / `with_agent` /
  `with_context` / `with_inputs` / `with_policy`) derives a **new frozen** Definition from a
  base on a single content-hash path — the receiver is never mutated, identical compositions
  collapse to one sha, and a skill or summon enters by **reference, not embed** (so the
  export checksum tracks the pin). **A name registry** (`DefinitionStore`) makes a name a
  **mutable pointer** over an **append-only, content-addressed object store**: `save` moves
  the pointer and appends a lineage event (frozen-only), `recall` is pure and never mints a
  sha, and `modify` / `reset` are the commit/checkout verbs (`reset` is a pure pointer move
  that refuses an unreachable sha). **Summonable knowledge** (`Wiki`) is a versioned,
  Merkle-hashed unit you pin by sha and `consult()` as **tainted** context — knowledge
  reaches the model as **data, never instructions** — with a per-page `TrustTier` that only
  ever raises suspicion. The retrieval half (`Rag`) ships as a **seam only**, locking in
  scrubbed embeddings and tainted, trust-tier-carrying hits. Only sealed, eval-mode values
  touch the world: `save` requires a frozen Definition, and `Wiki.mutable()` is rejected in
  eval mode.

See the [Refine & verify guide](docs/guide/refine-and-verify.md), the
[Compose guide](docs/guide/compose.md), the
[Train, calibrate & promote guide](docs/guide/train-and-tune.md), the
[Taming stochasticity guide](docs/guide/tameness.md), the
[Drive the language from the CLI guide](docs/guide/optimize-from-the-cli.md), the
[Agents as variables guide](docs/guide/variables-and-knowledge.md), the
[CLI reference](docs/guide/cli.md), the
[control-plane reference](docs/reference/refine-and-verify.md), the
[Tuner & learning reference](docs/reference/tuner-and-learning.md), and the
[release notes](docs/guide/release-notes.md).

These stand on the *foundational primitives* shipped earlier — substrate contracts, not
operators themselves:

- **A canonical Output content hash** — one content-identity primitive every ledger and
  replay path keys off.
- **An execution-coordinate cassette key** — record/replay now distinguishes each re-run
  of a leaf (sample, iteration, visit, depth), and folds tenancy into run identity.
- **A loop/program ledger** — per-`(item, edge, visit)` and per-recursion-depth resume
  with deterministic loop identity, so resuming a loop re-charges \$0 for work already done.
- **One cost model with a composition law** — a three-number interval
  (lower-bound / expected / worst-case) that multiplies along operator nesting.
- **A statistical gate algebra** — paired, variance-aware promotion gates plus a
  fail-closed precision gate for consequential guards, over a shared experiment-design spec.
- **Decode-knob ownership, a determinism tier, a correction corpus, and a Store-backed
  train-mode borrow** — the seams the Tuner and the operators need.

These foundations and their security and migration guarantees are documented in
[`docs/architecture/ARCHITECTURE.md`](docs/architecture/ARCHITECTURE.md) and
[`docs/architecture/SECURITY.md`](docs/architecture/SECURITY.md).

## Phase 2 — themes (next)

Phase 2 turns the local trust loop into something teams run together. Directions we're
exploring (priorities will shift with contributor and user input):

- **Cloud + scale by driver swap** — production `Store` / `ArtifactStore` / runtime
  backends behind the existing protocols, and a managed-deploy path beyond the local
  detached supervisor.
- **A connectors ecosystem** — more first-party and community Sources/Sinks, and a
  smooth path for contributing one (see
  [contributing a connector](docs/guide/contributing-a-connector.md)).
- **A knowledge hub** — promoting the company-brain primitive (built in Phase 1, not yet
  wired) into a shared, reusable knowledge surface across pipelines.
- **Deeper observability** — richer dashboards, alerting, and SLOs over the run-info
  surface.

## Get involved

The best first contribution is a **connector** — a self-contained Source or Sink that
doesn't touch the core seams. Start with
[`docs/guide/contributing-a-connector.md`](docs/guide/contributing-a-connector.md) and
[`CONTRIBUTING.md`](.github/CONTRIBUTING.md). Have an idea for Phase 2? Open a Discussion.
