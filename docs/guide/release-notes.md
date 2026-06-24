# Release notes

Notable, user-facing changes. For the exact public-symbol surface of any release, see the
[API reference](api-reference.md); for the longer arc, see the [Roadmap](../roadmap/README.md).

## Agent language — Milestone 6: variables & knowledge

An agent stops being a fixed artifact and becomes a **variable**: a content-addressed value
you compose from parts, name, and move through a version log — **git for agents** — and
knowledge becomes something you **summon** by reference as data. New, all importable from
the top-level `crawfish` package:

- **Copy-on-write composition — `with_*` (CRA-224, on the shared content-hash path of
  CRA-223).** `with_skill` / `with_agent` / `with_context` / `with_inputs` / `with_policy`
  each take a base Definition, deep-copy it unfrozen, apply one structural edit, and re-seal
  it through the **single** content-hash path — returning a **new frozen** Definition while
  the receiver is untouched. Two structurally identical compositions collapse to one sha
  (idempotent); any knob diff diverges it; `with_*` on a frozen receiver copies first
  (never raises), but mutating the **returned** object does. A skill or summon enters by
  **reference, not embed** (a version pin folded into `dependencies`), so `export().checksum`
  changes *iff* the pinned version changes. Consequential knobs (model, policies, Sink
  targets) stay static author config. New symbols: `with_skill`, `with_context`,
  `with_agent`, `with_inputs`, `with_policy`, `SkillRef`, `SummonRef`, `SummonMode`,
  `Summonable`.
- **Git for agents — `DefinitionStore` save/recall (CRA-225) + modify/reset (CRA-226).** A
  name is a **mutable pointer** into an **append-only, immutable, content-addressed object
  store** — git's exact ergonomic. `save(name, defn)` stores the body content-addressed
  (dedup), moves the `name → sha` pointer (the **sole** mutation), and appends a
  `DefinitionVersion` lineage event with the `parent` edge; it requires a frozen (eval-mode)
  Definition (`UnfrozenDefinitionError` otherwise). `recall(name)` / `recall(name, sha=...)`
  is **pure** — re-seals a stored object and **never mints a new sha**; `log` / `head` expose
  the lineage. `modify(store, name, fn)` is the commit verb (`recall → fn → save(parent)`,
  `fn` composing via `with_*`); `reset(store, name, to)` is the checkout verb — a **pure
  pointer move** that mints no object and refuses an unreachable sha
  (`UnreachableShaError`). Every plane is `org_id`-scoped. New symbols: `DefinitionStore`,
  `DefinitionVersion`, `modify`, `reset`, `UnfrozenDefinitionError`, `UnknownNameError`,
  `UnreachableShaError`.
- **Summonable knowledge — the `Wiki` (CRA-227); `Rag` deferred.** A `Wiki` is a versioned,
  content-hashed, summonable knowledge unit whose `content_sha` is a **Merkle over page
  leaves** (a re-hash re-derives only the changed page). `with_page` is copy-on-write; pages
  are **tainted by default** and carry a `TrustTier` (`TRUSTED`/`COMMUNITY`/`UNTRUSTED`) that
  only ever *raises* suspicion — never lowers taint. `readonly()` pins it into a Definition
  by a `SummonRef` carrying the **content sha, never the body** (the export checksum tracks
  the pin, so a secret body can't leak through the reference surface); `mutable()` is the
  train-mode edit handle, **rejected on a frozen (eval-mode) Wiki**. `consult()` materialises
  a `Context` whose entries are **tainted (fluid)**, so summoned knowledge flows through the
  fluid-data block and can never reach an instruction slot or a static-only Sink. Persistence
  rides the `Store` seam (a `ScrubbingStore` redacts secrets on write), tenancy-scoped by
  `org_id`. The retrieval half (`Rag`) ships as a **seam only** (`RagSeam` / `RagDeferred`),
  locking in scrubbed embeddings and tainted, trust-tier-carrying hits so the deferred impl
  can't regress them. New symbols: `Wiki`, `WikiPage`, `TrustTier`, `RagSeam`, `RagDeferred`,
  `WIKI_RECORD_KIND`.

Learn it: the [Agents as variables guide](variables-and-knowledge.md) (runnable, mirrors the
triage demo — compose a variant, save/recall by name, modify/reset across the version log,
consult a Wiki) and the
[Concepts → agents-as-variables half](concepts.md#the-agents-as-variables-half-compose-version-summon).

## Agent language — Milestone 5: the operator surface

The flagship slice completes. The control plane, composition surface, tunable-ML library, and
tameness layer were *libraries*; Milestone 5 makes the whole optimization plane drivable from
the shell and adds the two honesty primitives a self-optimizing app needs to trust what it
drives. New, all reachable from `craw` or importable from the top-level `crawfish` package:

- **The optimization plane on `craw` (CRA-219).** Five subcommands bind the already-shipped
  primitives — nothing re-implements a cost model, a search, or a gate. **`craw eval`** scores
  the frozen, eval-mode Definition against a Benchmark and gates on a named baseline (exits
  non-zero *iff* a metric regresses — drop it straight into CI). **`craw tune`** searches the
  knob space under the cost-regularized `Objective` and the variance-aware promotion gate, in
  train mode, byte-identical under `--seed`, budget-bounded. **`craw refine`** runs the
  verifier-gated `Refine` loop until a Rubric goal (`--until 'score>=0.95'`) or a bound.
  **`craw learn`** runs one eval-gated self-versioning cycle, or `--rollback <sha>` (a pointer
  move — *no model call*); a promotion/rollback emits an audit event. **`craw guard`** distils a
  closed-grammar predicate (parsed *as data*, never `eval`/`exec`) into a `HouseGuard` at its
  *earned* stage — a guard cannot self-promote to `block`. Every command shares
  `--budget/--seed/--org/--model/--live/--json`, is deterministic by default (the mock runtime —
  **no live model call** without `--live`), fires no Sink (the plane is egress-free), and emits
  a versioned, snapshot-tested `--json` schema.
- **The honest cost interval — `CostShape` / `compose_cost` (CRA-220).** `estimate_cost` was a
  silent lower bound, blind to the re-run multipliers of escalation, repair, retry, and
  `Refine` — a falsely-precise point that could only undershoot. `CostEstimate` now carries an
  honest band: `total_usd` (the lower bound, unchanged), `expected_usd` (with a CI), and
  `worst_case_usd`, with `total ≤ expected_lo ≤ expected ≤ expected_hi ≤ worst`. `CostShape` +
  `compose_cost` fold a nesting of operators *multiplicatively* (a `Quorum(5)` over an
  `Escalating(2×)` previews `10×`, escalation re-priced on the strong model);
  `CostShape.from_runtime` infers the shapes from the assembled wrapper chain. The contract is
  load-bearing: **the advertised `worst_case` is a true upper bound** a real run never exceeds.
  Pure static analysis — no model call.
- **Single-flight caching — `CacheStats.coalesced` (CRA-221).** A disk cassette only helps the
  *second* run; two identical items in one `Batch` both miss and both spend. `CachingRuntime`
  grows an in-process per-key `asyncio.Future` map so N concurrent identical callers share one
  computation: exactly **one `inner.run` ⇒ one `CostBudget.charge`**, a strict strengthening of
  the gas meter. Coalesced waiters charge `$0` and accrue their avoided spend into `saved_usd`;
  `CacheStats` gains `coalesced`, and `total`/`hit_rate` count it. A strict refinement (the
  coalescing key is the replay cassette key, so replay is bit-for-bit either way) and
  tenant-safe (the key is salted with `org_id` — org A's computation is never served to org B);
  an in-flight exception reaches every awaiter and clears the key so a retry recomputes.
- **The dependency resolver + lockfile — `resolve` / `Lockfile` / `SemVer` (CRA-222).** A
  Definition *summons* units by reference at a version constraint; an unpinned transitive
  closure breaks replay reproducibility. A pure, offline resolver walks the closure, picks the
  highest compatible version (`^`/`~`/exact ranges), detects conflicts (naming both requirers)
  and cycles, and pins every ref to an exact version + `sha256:` integrity. `Lockfile` records
  one small `closure_sha()` — the reference a run embeds. Reading a lockfile is **data-only**
  (it never executes unit code) and re-verifies the recorded sha, **failing closed** on a
  tampered file or an unknown `LOCKFILE_VERSION`; a mutated unit diverges the closure_sha, so an
  un-versioned mutation can't enter a frozen closure. Driven from `craw lock` (`--check` is the
  fail-closed CI drift gate). New public symbols: `resolve`, `Lockfile`, `Pin`, `SemVer`,
  `CandidateSource`, `InMemoryCandidateSource`, `ResolutionError`, `read_lockfile`,
  `write_lockfile`, `LOCKFILE_VERSION`.

Learn it: the [Drive the language from the CLI guide](optimize-from-the-cli.md) (runnable,
mirrors the triage demo — score and gate, the honest cost band, single-flight coalescing two
in-flight calls, and a committed lockfile), the [CLI reference](cli.md), and the
[Concepts → operator surface](concepts.md#the-operator-surface-drive-price-and-pin-the-language).

## Agent language — Milestone 4: taming stochasticity

The one stochastic primitive — a model `Run` — gets bounded *itself*. Four disciplines layer
onto any producing step while keeping every determinism, typing, and taint guarantee: vote it
down, let it decline, distil its invariants into a pure guard, constrain its surface. New, all
importable from the top-level `crawfish` package:

- **`QuorumRuntime` — self-consistency as a typed operator.** Wraps any inner `AgentRuntime`,
  samples the same `RunRequest` `k` times (each a distinct seeded leaf charging the shared
  budget, replayable under its own cassette via the execution coordinate), and reduces by a
  **pure** consensus vote. `run(...)` is the plain runtime seam (winner text); `run_quorum(...)`
  returns a `QuorumResult` — winner, **aggregate taint**, the `ConsensusResult` tally, and the
  `Sample`s. `majority_vote(field=...)` is the modal-output estimand with mandatory
  canonicalization; an ill-defined plurality abstains to a *declared* `default_text` or raises
  `QuorumAbstention` — never a silent pick. `k` defaults to the tunable `sample_k` knob, and a
  sequential proportion test (Wilson lower bound on the leader's share > 0.5) stops early with
  no peeking penalty. `quorum_output(...)` wraps the winner into a typed Output; a vote **never
  launders taint** (winner tainted iff any sample was). Also: `ConsensusFn`, `MajorityVote`.
- **`abstain_below` / `abstain_below_calibrated` — abstention as a typed Output value.** A
  first-class "I decline to answer" that is a routable **value**, not an exception or a magic
  string. `abstain_below(threshold)` measures the run's self-reported confidence (fluid data,
  never an instruction) and either passes a confident Output through unchanged or returns a
  fresh Output (via `Output.derive`, so taint + lineage propagate) carrying an `Abstention`
  tagged `ABSTENTION_MARKER` *in the JSON*. `is_abstention(value)` is a pure, total predicate —
  hand it to a `Classifier` so a `Router` branches a decline to review. Fail-safe (a missing
  confidence declines) and idempotent. `abstain_below_calibrated(report)` reads the threshold
  off the `calibrate` reliability curve instead of guessing a constant — the sound default.
- **`HouseGuard` — learned-then-distilled deterministic guards.** A program accretes its own
  invariants: quality is learned stochastically (`propose_rule` emits a FLUID candidate from
  one model `Run`), distilled to a pure predicate (`distill` parses it *as data* into a closed
  grammar — `Comparison | SetMembership | NumericBound | BoolCombination | Always`, no
  `eval`/`exec`; the proposal can only *select within* the grammar, raising `GuardGrammarError`
  on an attempt to widen it), and only **earns** enforcement after a **joint** precision/coverage
  gate. `HouseGuard.synthesize(...)` mints a `GuardCertificate` reporting a Wilson precision
  *lower* bound **and** coverage with a CI; graduation needs both to clear their floors over a
  non-empty corpus, so a 99%-precision / 2%-coverage rule cannot block. Fails closed
  (`GuardNotEarned`), runs a `shadow → warn → block` lifecycle (`GuardStage`), is content-hashed
  and reversible, and exposes the distilled rule as a pure `Metric` (`PredicateMetric`, 0/1, $0).
  Stats helper `wilson_lower_bound`; range type `Interval`.
- **`Grammar` — constrained / grammar-guided decoding as a per-call property.** Strictly
  stronger than validate-and-repair: tell the runtime the output shape up front and a malformed
  value is an *impossible* state, not a repaired one. Build via `Grammar.enum` /
  `Grammar.regex` / `Grammar.json_object` / `Grammar.from_output_schema` (frozen, declarative,
  one field); `enforce(text)` is a **pure** projection onto the constraint surface (snap to an
  enum member / first regex match / recover a balanced object), raising `GrammarError` only when
  no candidate exists at all — never a silent coercion. Attaching a grammar to a `Run` keeps
  `repair_count` at **0**. The grammar is **static / trusted** (no constructor reads a fluid
  value) and rides on the per-call request, kept out of the Definition content hash — the
  prompt-injection boundary holds. `GrammarKind`; `parse_grammar` is the inverse of
  `to_request_grammar`.

Learn it: the [Taming stochasticity guide](tameness.md) (runnable, mirrors the triage demo —
ambiguous ticket voted on, low-confidence declined to review, house-guard blocks a disallowed
label, structured field under a grammar) and the
[Concepts → taming the stochastic primitive](concepts.md#taming-the-stochastic-primitive-vote-decline-distil-constrain).

## Agent language — Milestone 3: the tunable-ML library

The **flagship** half lands: an agent is now a *model with tunable weights*, and `mutable` is
the train/eval switch. Crawfish gains the PyTorch-for-LLMs surface — measure run-to-run noise,
search the knob space under a cost-regularized objective, promote only past that noise, and
transfer the learned weights — without giving up a single determinism, typing, or versioning
guarantee. New, all importable from the top-level `crawfish` package:

- **`train()` / `eval()` / `guard_consequential()`** — the two-axis mode unifier. *Which*
  knobs may move (Axis 1, `tunable`) and *whether* the artifact is sealed (Axis 2, mode) are
  now orthogonal, mirroring PyTorch's `requires_grad` vs `.eval()`. `train(d)` is an unfrozen,
  copy-on-write training copy; `eval(d)` re-freezes via the content hash (so `eval(train(d))`
  is idempotent). `guard_consequential(d)` is the load-bearing gate: **a Sink write or a
  recorded run is eval-only** — a training artifact has no stable identity to key idempotency.
- **`TuneSpec` / `KnobDomain` / `tune_spec_sha`** — the tunable knob space as *data*, authored
  as `tune.toml` and folded into the Definition's content hash. **Changing the search space
  versions the agent** (an empty `tune.toml` stays hash-neutral). Pinned knobs (`tunable=False`)
  are never proposed.
- **`calibrate(...) → CalibrationReport`** — runs each golden case `runs` times under distinct
  derived seeds and reports the **noise band** (`rubric_std`), structural `output_variance`,
  Brier / ECE-with-CI calibration, a reliability curve, and an **evidence-derived**
  `abstention_threshold`. Refuses a replay runtime (it would fabricate zero variance) and
  honours the autonomy ceiling (`partial=True` on a budget/cancel breach). `extract_confidence`
  / `abstention_threshold` (in `crawfish.escalate`) replace the old guessed escalation constant
  with one read off measurements.
- **`Objective` / `ObjectiveForm` / `ObjectiveScore`** — a cost-regularized loss
  (`Σ wᵢ·scoreᵢ − λ·cost − μ·ece`) the Tuner maximizes **only among candidates that already
  pass the hard regression gate**, so cost can break a tie or veto a marginal gain but can
  **never** promote a quality regression. `cost_weight=0` reproduces today's winner; an
  ε-constraint form and a Pareto mode are available.
- **`promote_against_baseline(...) → PromotionVerdict`** — the variance-aware promotion gate:
  promote only when the primary gain **clears the noise band** (`k·std`) *and* no metric
  regresses past its own band. Reduces byte-for-byte to the single-point gate when no `std` is
  recorded, so every existing baseline keeps working. `save_baseline_from_report` /
  `load_baseline_std` carry the band; a `fresh_sample` corrects the winner's curse.
- **`state_dict()` / `load_state()` / `StateDict` / `RoleKnobs`** — the architecture/weights
  split (*Hugging-Face-for-agent-weights*). Extract the tunable knobs as JSON-only weights
  (per-role knobs, coordination choice, few-shots, summons as references-by-version — **no**
  architecture, **no** embedded Definition) and transfer them onto a sibling of the same shape.
  `strict=True` refuses a shape mismatch (`IncompatibleStateError`); `strict=False` loads the
  intersection; `only=[...]` transfers named groups. Copy-on-write — a new frozen artifact.
- **`ServingLoop` / `ExploreSchedule` / `ExploreStrategy`** — the serving-time explore dial.
  Routes `(1-ε)` of live items to the promoted best and `ε` to a trial candidate, choosing
  *which* items explore by a seeded hash of the recorded `item_id` (a replay re-explores
  exactly the same items). Decaying-ε, budget-bounded, and `graduate`s only after a
  **pre-registered sample size** (no peeking) and only through the eval gate. **Only static
  knobs are ever promoted** — the learning loop stays inside the security spine.

Learn it: the [Train, calibrate & promote guide](train-and-tune.md) (runnable, mirrors the
triage demo) and the [Concepts → PyTorch-for-LLMs half](concepts.md#the-pytorch-for-llms-half-train-eval-and-the-tunable-knob).

## Agent language — Milestone 2: the composition surface

The control plane gains *shape*. Agent work that branches, cycles, and recurses is now a
typed, durable graph — bounded, taint-tracked, and crash-resumable for **\$0**. New, all
importable from the top-level `crawfish` package:

- **`branch(classifier, branches)`** — makes a `Router` a first-class, **runnable**
  composition step: each item is classified and dispatched through the same step machinery
  as its branch, so a branch may be a `Sink`/`Batch`/`Filter`/`Aggregator` and inherits
  the identical budget / taint / checkpoint guarantees. The label set is closed and
  totality-checked at construction; `check_types` verifies every branch accepts the
  upstream output.
- **`Program`** — a `Workflow` whose **edges may cycle**. Register nodes with `.step(...)`,
  wire directed edges with `.edge(...)`; a back-edge re-enters its region while a guard
  predicate holds. Every traversal is a content-addressed version transition (no in-place
  mutation), metered into one shared `CostBudget`, with taint carried across every edge.
  Cycles are bounded by `max_visits` / budget / cancel / calibrated no-progress — **never
  wall-clock** — and a back-edge with no `max_visits` raises **`UnboundedCycleError`** at
  assembly. `Edge`, `ProgramResult` (`output`, per-edge `visits`, `stopped` reason).
- **Durable `$0` resume for cycles** — pass a shared `Store` and `resume=True`, and a
  `Program` that crashes mid-iteration re-derives the committed iterations at `$0`. Resume
  is content-hash *verified*: each iteration's `produced_by` is the deterministic
  `{region_version}#{edge_id}#{visit}` coordinate, so the replay reproduces the checkpoint
  bit-for-bit. Every ledger row carries `org_id`.
- **`recurse(body, *, base_case, max_depth, combine)`** — bounded self-referential
  invocation: a depth-guarded back-edge re-entering the same **frozen** `Definition`,
  folding the descent-order children with an existing reducer. `max_depth` is mandatory
  (**`UnboundedRecursionError`** otherwise) and the whole-tree shared budget guards the
  `O(b^d)` fan-out; a fold **never launders taint** (the reduced Output is tainted if any
  child input was). `Recurse`, `RecurseResult` (`output`, `depth_reached`, `stopped`).

Learn it: the [Compose guide](compose.md) (runnable, mirrors the triage demo's
branch-by-type and bounded recurse) and the [Concepts → composition
surface](concepts.md#the-composition-surface-branch-cycle-recurse).

## Agent language — Milestone 1: the control plane

The first headline operators of the [agent language](../roadmap/README.md#milestone-1-the-control-plane-shipped)
land: a bounded, metered, durable **iterate-until-goal** loop, gated by a critic that has
to *earn* the authority to stop it. New, all importable from the top-level `crawfish`
package:

- **`Refine`** — run a producing `Definition`, check each frozen `Output` against an
  external `StopCondition`, and iterate until satisfied or a bound is hit (`max_iters`,
  the shared `CostBudget`, cooperative cancel, or noise-aware no-progress — never
  wall-clock). It mutates nothing and folds the three fixed-bound re-run atoms
  (`EscalatingRuntime`, `Run._repair`, `RetryPolicy`) into one goal-driven operator.
  `RefineResult` reports the real `spent_usd`, the iteration count, and why the loop
  stopped. `feature_loop(...)` is a keyword-only alias.
- **Stop conditions** — `RubricThreshold` (a metric clears `at_least`), `PredicateStop`
  (a typed predicate), and `VerifierStop` (a gated critic accepts). Building a `Refine`
  whose critic shares the body's content hash is rejected: the generator may never
  critique itself.
- **`Verifier`** — a critic over a closed label set with a mandatory `default`. Gating
  authority is **typed**: a bare `Verifier` is in `WARN`/`SHADOW` and cannot stop a loop.
  `Verifier.gated(...)` is the only path to a `GatedVerifier` (stage `BLOCK`), and it
  **fails closed** — a never-benchmarked critic, or one below `min_precision` against a
  decision `GoldenSet`, raises `VerifierNotGated` rather than being trusted to block
  production. `Verdict` carries taint forward; `VerifierStage` is the
  `SHADOW`→`WARN`→`BLOCK` lifecycle.
- **`$0` crash-resume for loops** — pass an `ExecutionLedger` and `resume=True`, and a
  `Refine` loop that crashes mid-iteration restarts at the next iteration re-paying `$0`.
  Resume is content-hash *verified*: each iteration's `produced_by` is the deterministic
  `body.content_sha()#visit` coordinate, so the replayed Output reproduces the checkpoint
  bit-for-bit. Every ledger row carries `org_id`, so a cross-tenant resume is isolated.

Learn it: the [Refine & verify guide](refine-and-verify.md) (runnable, mirrors the triage
demo) and the [control-plane reference](../reference/refine-and-verify.md).
