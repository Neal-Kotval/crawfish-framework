# The Agent Language Epic

> Crawfish as a deterministic programming language for agents **and** a tunable ML library — "PyTorch for LLMs, one level up." This epic is the full, super-specced backlog: every milestone, every issue, grounded in `file:line` and the two architecture docs, with every reviewer blocker/major fix applied (and noted).

---

## 1. Epic summary

Crawfish is two halves of one thesis. The **language half**: the agent call is the only stochastic primitive (one `AgentRuntime.run` leaf, `runtime/base.py:1-32`); everything around it — control flow, gating, scoring, promotion — is pure deterministic Python over typed (`core/types.py`), versioned (`versioning/version.py:21-62`), taint-tracked (`Flow.STATIC/FLUID`, `core/types.py:36-46`) values, funneled through three swappable seams (`AgentRuntime`/`Store`/`ArtifactStore`) and one `RunContext` carrying `CostBudget` + `CancelToken` (`core/context.py:74-97`). The **ML-library half**: a `Definition` is the `nn.Module` (`definition/types.py:109`), its `AgentSpec` knobs are the parameters, `Tuner` is the optimizer (`tuner.py:392`), `Rubric`/`Benchmark` are the loss (`metrics.py:461,479`), `GoldenSet` is the dataset (`eval.py:73`), and `LearningLoop.improve` is `optimizer.step()` with an eval gate (`learning.py:195`). **The unifier:** the `mutable` property *is* train/eval mode. In **train** mode a Definition is unfrozen — its `tunable` knobs are `requires_grad` and the optimizer may propose mutations (the `Tuner._with_agents` unfrozen-copy path, `tuner.py:119-122`). In **eval** mode it is frozen, content-hashed, and replays bit-for-bit (the `_refreeze` path, `tuner.py:102-116`) — the only mode in which a consequential Sink may fire. Every mutation routes through content-hashed copy-on-write that mints a new `Version.sha`; un-versioned mutation is forbidden. This epic builds the missing unifier, the iterate-until-goal + cyclic control plane, the optimization-plane surface, and the revolutionary capabilities that fall out — without ever moving stochasticity off the leaf.

---

## 2. Why now / what it unlocks

The North Star is **making Crawfish a trustable emission target**: a stochastic generator (`craw code`, the planned Claude Code fork — the `ccexport.py` `export_claude_code`/`definition_to_cc_agent` seam already exists) writes agents *into* Crawfish, and the language guarantees the result is reproducible, non-leaking, cost-bounded, and quality-gated. That only works if `craw code` can reach the framework through the shell the way it reaches `craw manage` (`cli.py:431-448`) — but today **the entire optimization plane is libraries with no CLI** (`AGENT-LANGUAGE-AUDIT.md §3`): `eval.py`/`tuner.py`/`learning.py` ship no subcommand. This epic closes that, and ties directly to the four goals:

1. **Deterministic language** — the iterate-until-goal operator (`Refine`), cyclic `Program`, and `recurse` give the control plane its missing constructs while keeping the leaf the sole stochastic site. Closes audit Gaps #1–#4.
2. **Tunable ML library** — `train()`/`eval()` + per-knob `tunable`, `state_dict()`/`load_state()`, a cost-regularized `Objective`, `cw.calibrate()`, and a variance-aware promotion gate make the existing optimizer ergonomic and statistically honest.
3. **Cost honesty / safety** — a worst-case cost interval (closing Gaps #5/#9), single-flight live caching, and budget-everywhere make unattended self-optimization safe to trust.
4. **Security as a type** — lifting `Flow`/taint from a runtime check to an assembly-time check (and, as a moonshot, a non-interference certificate) makes prompt injection a structural error, not a runtime hope.

---

## 3. Grounding: what exists today vs what is NEW

| Capability | Status | Anchor |
|---|---|---|
| `Flow` static/fluid taint effect system (injection boundary) | **EXISTS** | `core/types.py:36-46,60` |
| `CostBudget` gas (hard-kill + `remaining_usd` preflight), `CancelToken`, `RunContext` | **EXISTS** | `core/context.py:32-71,74-97` |
| Content-hash `Version` + `Freezable` (frozen rejects mutation) | **EXISTS** | `versioning/version.py:21-62` |
| Three seams: `AgentRuntime` ABC, `Store`, `ArtifactStore` | **EXISTS** | `runtime/base.py:1-32` |
| `Definition` (Freezable, content-hashed `export()`) + `AgentSpec` typed knobs | **EXISTS** | `definition/types.py:40-52,109-145` |
| `Tuner` (pure seeded mutators, regression-gated) + `LearningLoop` (eval-gated, reversible) | **EXISTS** | `tuner.py:1-85,392`, `learning.py:1-99,195` |
| `Rubric`/`Metric`/`Benchmark` + `gate_against_baseline`/`is_regression` | **EXISTS** | `metrics.py:461,479,532-546`, `eval.py:241-253` |
| `GoldenSet`/`EvalCase` + `capture_case` | **EXISTS**; `from_corrections` is **NEW** | `eval.py:56-126` |
| `Batch` fan-out (shared budget, bounded concurrency) | **EXISTS** | `batch.py` |
| `Router`/`Classifier` (closed labels, mandatory default) | **PARTIAL** — not a runnable Workflow step | `nodes/router.py:190-214`, `workflow.py:187` |
| `Workflow.run` (forward-only, assembly type-check, per-step checkpoint) | **EXISTS** — linear/cycle-free only | `workflow.py:68-187` |
| `EscalatingRuntime` (confidence cascade, exactly 2 attempts) | **PARTIAL** | `runtime/escalate.py:78-109` |
| `Run._repair` (exactly one re-prompt, preflights, feeds error as FLUID) | **EXISTS** — single-shot | `run.py:309-337` |
| `RecordReplayRuntime`/`CachingRuntime` (cassette key `sha256(version+inputs)`, $0 hit) | **EXISTS** — no live single-flight | `cache.py`, `runtime/replay.py:25-39` |
| `estimate_cost` (static, same resolver as runtime) | **EXISTS** — blind to escalate/repair/retry multipliers | `cost.py` |
| `Output` (frozen-pydantic, `derive()` propagates taint/lineage) | **EXISTS** — *frozen, see fix below* | `output.py:28-66` |
| `ExecutionLedger` (`checkpoint_step(int)`/`completed_steps`) | **EXISTS** — int-step keyed (loop schema is NEW) | `ledger.py:31-122` |
| iterate-until-goal operator (`Refine`) | **NEW** | — |
| cyclic composition surface (`Program` w/ back-edges) | **NEW** (`BatchExecutor` raises `CycleError`, `executor.py:85-87`) | — |
| train/eval mode + per-knob `tunable` flag (the unifier) | **NEW** | — |
| `state_dict()`/`load_state()`, `cw.calibrate()`, `Objective`, explore dial | **NEW** | — |
| bounded `recurse` | **NEW** | — |
| `Verifier`, `Quorum`, `Abstention`, constrained decoding | **NEW** | — |
| `with_skill`/`with_context`/`with_agent`, `save`/`recall`, `modify`/`reset`, `Wiki`/`Rag` | **NEW** | — |
| `craw eval/tune/refine/learn/guard`, single-flight cache, dependency resolver/lockfile | **NEW** | — |
| property/capability algebra (`Grade`) generalizing `Flow`/`Freezable` | **NEW** | — |

---

## 4. Revolutionary Capabilities — the headline frontier bets

These are *bets*, cross-referencing the deep specs in the milestones below rather than re-specifying them. Each routes mutation through content-hashed versioning, keeps the stochastic part at the leaf, and gates every promotion on the eval gate.

| # | Bet | Unlocks | Feasibility | Risk |
|---|-----|---------|-------------|------|
| **R1** | **Git-for-agents**: content-addressed, diffable, mergeable `Program` (lift `_content_sha` from Definition to the program graph; `craw diff`, `craw merge`) | PR-reviewable agent changes; the substrate every other bet keys off | High (~80% built: `Version`/`Freezable`/`_content_sha`/`VersionRecord` lineage) | Lifting the hash from Definition to the program graph; merge granularity on prompt text |
| **R2** | **Injection-impossible-by-type**: assembly-time non-interference check + `craw prove --no-injection` certificate | Enterprise/regulator-grade security claim no competitor can match | **Moonshot** | **Is the 2-point lattice statically decidable over the dataflow graph?** Spike-gated (see fix) |
| **R3** | **Counterfactual time-travel replay** (`craw replay --swap`): re-run a historical run vs a candidate, replaying unchanged leaves at $0 | Re-run yesterday's 10k items against a candidate fix for near-$0 | High (change-detection over existing cassette path) | **Upstream-change cascade** must be cost-bounded (see fix) |
| **R4** | **Programs that ship their own learned-then-distilled guards** (house-guard generalized): propose stochastically → distill to a pure predicate → earn enforcement via the gate | Self-hardening artifact; "learn stochastically, enforce deterministically" | High | Corpus bias; precision-AND-coverage certificate honesty |
| **R5** | **Hugging-Face-for-agent-weights**: typed, content-hashed, references-by-version `state_dict` decoupled from architecture | Network-effect ecosystem; quality decoupled from architecture | High (~90% built) | Summoned-unit reference resolution; decode-knob ownership |

**North-star framing (not a scheduled milestone):** these five compose into *craw code as a self-hosting compiler* whose source is natural-language intent and whose target is a type-checked (R2), content-addressed (R1), cost-bounded, quality-gated Crawfish `Program`. Generation is stochastic; every promotion is eval-gated and reversible. This is the closing vision, not independent work.

**Sequencing of the bets:** R1 first (Milestone 0 — mostly built, unblocks everything). R3 as the demo that sells R1 (most visceral, near-zero new stochastic machinery). R2 as the flagship differentiator (**spike the decidability question before committing XL effort**). R4 and R5 ride on R1 + the shared variance-aware gate / two-axis-mode primitives.

---

## 5. Foundational prerequisites (Milestone F)

> **Reviewer-driven addition.** Multiple reviewers (PL-correctness, shipping-pragmatist, security) flagged that several issues treat NEW machinery as if it existed, and that shared schemas (cassette key, ledger, gates, `Output` hashing, corrections corpus) are edited by 2–3 work-streams at once — violating one-owner-per-file. These prerequisites resolve those once, up front. **No downstream issue is "ready" until its prerequisites here are merged.**

### F-0 — `Output` content hashing (correct the grounding)

**Fix applied (SECURITY blocker; PL blocker):** Reviewers split on a factual point. The security reviewer is correct: `Output` is **already frozen** (`output.py:44`, `model_config = {"frozen": True}`) and `derive()` already mints a fresh immutable `Output` with a new `id` and propagated taint/lineage (`output.py:46-66`). The PL reviewer is correct that there is **no `sha`** on `Output`. **Resolution:** do *not* add a second freezing path and do *not* make `Output` mutable. Add a pure helper `output_content_sha(o: Output) -> str = sha256(o.model_dump(mode="json"))` and have back-edge/iteration version transitions reuse `Output.derive()` plus this sha for the ledger key. This single primitive is the prerequisite for every "mint a new frozen Output sha / no-progress-by-sha / output_sha in ledger" claim in CL-1/CL-4/C2/C3/TS-1/R1/R3.
**Determinism:** pure function over a frozen value; no mutation, no model call. **Effort:** S. **Owner:** versioning/output. **Acceptance:** `output_content_sha` is stable across processes; two structurally-equal Outputs hash equal; `derive()`-produced Outputs carry propagated taint and a distinct `id`.

### F-1 — Canonical cassette-key / execution-coordinate schema (single owner of `runtime/replay.py`)

**Fix applied (SECURITY blocker on TS-1/R3/TS-8; PL minor; shipping minor):** Today `_key(request) = sha256(id+version+role+model+inputs+session_id)` (`runtime/replay.py:25-39`) has **no execution coordinate**. Quorum's k identical samples would collide into one cassette (unanimous no-op), `Refine`-under-replay would collide across iterations, and TS-8's `temperature`/`decode_seed` would sit *outside* run identity (two decode settings replay identically). **Resolution:** one owner of `runtime/replay.py` defines a single versioned extension: `_key` folds an optional, content-hashed **execution coordinate** `{sample_index?, iter_index?, visit_count?, depth?}` and any decode-control field that is *not* already in the Definition's content hash. Legacy unsalted cassettes still resolve (coordinate absent ⇒ today's key). Every operator that re-runs the leaf records its coordinate into run identity.
**Determinism:** same coordinate ⇒ same cassette ⇒ bit-identical replay. **Effort:** M. **Owner:** runtime/replay. **Acceptance:** k recorded quorum samples produce k distinct cassettes; same `(version, inputs, coordinate)` replays identically; legacy cassettes still hit; decode fields that enter `_key` cannot be set independently of run identity.

### F-2 — Loop/program ledger schema (composite key) — NEW, not "reuse"

**Fix applied (PL major):** `ledger.checkpoint_step(pipeline_id, step_index:int)` / `completed_steps -> set[int]` (`ledger.py:58-68`) cannot represent per-`(item, edge, visit)` progress or a per-item depth stack. The drafts mislabel this as "reuse verbatim." **Resolution:** add an explicit extended ledger key space `(loop_id, item_id, edge_id, visit) -> output_ref` (and a depth variant for `recurse`), with a migration, flagged **NEW**. `loop_id` is **deterministic** (hard requirement, not a risk): `loop_id = sha256(body.version.sha + item.lineage + edge_id)` — never `new_id()` (security minor: forbid `new_id()` so resume re-charges $0 for completed iterations).
**Determinism:** every row carries `org_id`; resume re-derives the same `loop_id`. **Effort:** M. **Owner:** ledger. **Acceptance:** two process invocations of the same loop over the same item produce the same `loop_id`; resume re-charges $0 for completed iterations; cross-`org_id` isolation holds.

### F-3 — The gate algebra (single owner of `eval.py` gate; reconcile three gate notions)

**Fix applied (PL missing; ML major; CL-2 blocker root cause):** Three different gates are now in play and the drafts conflate them: (a) **relative-regression** `gate_against_baseline`/`is_regression` (exists, `eval.py:241`, `metrics.py:532`); (b) **variance-aware** LCB-vs-baseline (AL-T5/TS-2); (c) **absolute-precision** for verifiers/guards (CL-2/TS-3/R4). **Resolution:** one issue defines the gate algebra and which consumer uses which. Critically, the ML reviewer's statistical corrections are adopted as hard requirements for (b) and (c):
- Use a **paired** test over per-case score deltas (paired bootstrap / paired-percentile CI on the mean delta), exploiting that baseline and candidate see the *same* `GoldenSet` cases — not an unpaired normal band.
- Apply a **family-wise correction** (Holm) across the Rubric's metrics, **or** designate one primary metric with the rest as pre-registered non-inferiority guardrails.
- **Drop Clopper-Pearson for continuous `[0,1]` rubrics**; reserve it only for genuinely binary metrics (pass/fail). Prefer **Brier/NLL** as primary calibration metrics; ECE is a diagnostic with a bootstrap CI.
- `k` (noise-band multiplier) is **derived from a stated α**, not a free constant.
- The absolute-precision gate (c) **fails closed**: no baseline / never-benchmarked ⇒ reject.
**Determinism:** pure arithmetic over recorded scores + recorded std; same inputs ⇒ same decision. **Effort:** M. **Owner:** eval/metrics gate. **Acceptance:** `std=0,k=0` reproduces today's `is_regression` byte-for-byte; a candidate within the paired noise band is rejected; a rich rubric does not inflate false-promotion past α after correction; the precision gate raises when no baseline exists.

### F-4 — `GoldenSet.from_corrections` + a `correction` ledger event kind

**Fix applied (shipping major):** Four issues (CL-2, TS-3, TS-7, R4) depend on `GoldenSet.from_corrections` sourcing `human_revert`/`ci_failure`/`review_reject` from the Store ledger, but it is orphaned. **Resolution:** promote it to a foundational issue plus a first-class `correction` emission kind so the source set is explicit and queryable. Until it lands, every dependent accepts an explicitly-authored `GoldenSet` (uniform).
**Determinism:** deterministic given a fixed ledger; carries `org_id`. **Effort:** S/M. **Owner:** eval. **Acceptance:** count matches the ledger; cross-org isolation; a `correction` kind is queryable.

### F-5 — Decode-knob ownership ADR (`temperature`/`top_p`/`decode_seed`/`grammar`)

**Fix applied (PL major; ML major; SECURITY major on TS-8):** AL-T1 wants `temperature`/`sample_k` as first-class tunable `AgentSpec` fields (in the content hash); TS-8 puts `temperature`/`decode_seed` on `RunRequest` and argues *against* putting `grammar` in the hash. Two subsystems would each believe they own the same decode parameter. **Resolution (one ADR, prerequisite for AL-T1/AL-T2/TS-8):**
- **Tunable decode knobs** (`temperature`, `top_p`, `sample_k`) live in the **Definition/`AgentSpec`** (enter the content hash, are what the Tuner searches and `state_dict` serializes). The `RunRequest` field is **derived** from the resolved Definition at call time — never independently set. ("Temperature appears in exactly one authoritative location; the other is derived" is an acceptance criterion.)
- **`grammar`** is a per-call `RunRequest` property (keeps provider dialects out of the content hash) and degrades gracefully (TS-8).
- Any decode field that affects output **must** enter run identity: either via `version.sha` (knob path) or via the F-1 `_key` extension (`decode_seed`).
- The `AgentRuntime` contract declares a **determinism capability tier** (`honors-seed` / `best-effort` / `none`); `cw.calibrate()` records the tier so model-stochasticity is not conflated with infra-nondeterminism, and attributes a **variance floor** to infra when the backend is non-deterministic.
**Determinism:** strengthens it (no decode field escapes run identity). **Effort:** S (ADR) + the field additions. **Owner:** definition/runtime. **Migration note:** adding `temperature`/`sample_k` to `AgentSpec` and `tune`/`summons` to `Definition` changes every existing frozen artifact's sha — ship behind a content-hash version bump with a documented re-freeze.

### F-6 — Cost-model single owner (`cost.py`) + one composition law

**Fix applied (shipping major; SECURITY minor; PL/ML on expected-band):** Three issues (CL-3, ALG-5, Surfaces ISSUE-2) all edit `estimate_cost`/`CostEstimate` with different designs — violating one-owner-per-file. **Resolution:** one cost-plane issue owns `cost.py` (Surfaces ISSUE-2 is the canonical spec); CL-3 and ALG-5 become *consumers*, not editors. The composition law is **multiplicative along operator nesting**: `worst_case = Π(per-operator multiplier)` (refine `max_iters` × escalate 2 × quorum `k` × retry `n` × recurse `b^max_depth`), folded through the *same resolver the runtime uses*. The existing scalar `total_usd` stays unchanged (the lower bound); `expected_usd`/`worst_case_usd` are additive fields. `expected` is a **CI-aware band** (rates from `cw.calibrate`/ledger carry uncertainty), never a falsely-precise point; with no rates, `expected == worst_case` (never undercount). See ISSUE OPT-2 for the full spec.

### F-7 — Borrow-lifetime / mode operational semantics

**Fix applied (PL major; SECURITY major; PL missing):** ALG-4's "statically unaliasable exclusive borrow" overclaims — its enforcement is a runtime registry (dynamic, racy across async). **Resolution:** downgrade the claim to **dynamic exclusive borrow with an atomic acquire**, and specify lifetime via an explicit **context-manager protocol** (`with defn.mutable() as m:` acquires on enter, releases on exit). The borrow registry is a **Store-backed atomic claim** reusing `claim_idempotency`'s atomic, tenancy-scoped, race-safe pattern (`sink.py:143`) — *not* an in-process dict. Mutable borrows are confined to a `train()` context that cannot span concurrent runs. See ISSUE ALG-4.
**Acceptance:** a concurrent-acquire race (two `mutable()` on one object across async tasks) raises `ExclusiveBorrowError` deterministically; sequential acquire/release round-trips.

### F-8 — Experiment-design spec (shared by calibrate/gate/quorum/explore/guard)

**Fix applied (ML missing — "the single biggest ML rigor gap"):** one shared spec all statistical consumers inherit: estimand definitions; primary-vs-guardrail metric designation; **pre-registered sample sizes or anytime-valid sequential bounds** (so online loops control optional-stopping/peeking error); **paired** tests over `GoldenSet` cases; **family-wise correction**; **held-out tune-set vs gate-set split** (the Tuner must not gate on the set it searched — closes the optimizer-overfits-the-eval hole); **power / minimum-detectable-effect** guidance for `GoldenSet` sizing; and **winner's-curse correction** (re-estimate or shrink a promoted argmax's score on a fresh sample before storing it as the new baseline, so the bar does not ratchet up on noise).
**Determinism:** all of it is pure post-hoc analysis of recorded runs. **Effort:** M (spec) + adoption hooks. **Owner:** eval/metrics. This is a *cross-cutting acceptance gate*: no gate ships until it conforms.

---

## Milestone 1 — Control Plane: iterate-until-goal (the flagship operator)

> Generalizes the three fixed-bound re-run atoms (`EscalatingRuntime` = 2×, `run.py:99-109`; `Run._repair` = +1, `run.py:309-337`; `RetryPolicy` = on-exception-only, `retry.py`) into one goal-driven, budget-bounded, taint-carrying, crash-durable operator. Closes audit Gap #1 and Gap #3 (`spent=0.0`). **Reviewer dedup note:** `Refine` was specced three times (CL-1, TS-5, `craw refine`). **CL-1 is the canonical operator; TS-5 folds into it; `craw refine` is its CLI in OPT-1.**

### ISSUE CL-1 — `Refine`: the verifier-gated iterate-until-goal operator (NEW)

**Problem.** No way to express "keep running until good enough, but never past N tries or $X." Hand-rolled `while` loops bypass the shared `CostBudget` (a fresh `Run` defaults an unbounded `CostBudget()` via `RunContext`, `context.py:82`), lose per-iteration checkpointing, and report `spent=0.0` (Gap #3). `Refine` makes the bounded, metered, durable loop the easy path.

**API design (NEW).** A `Node` with the inner `Definition`'s IO schema plus a `StopCondition` ABC. The stop signal is **external** — a `Rubric` threshold, a typed predicate, or a gated `Verifier` (CL-2) — **never the generator critiquing itself** (assembly-time check: `critic` must be a distinct `Definition` version from `body`).

```python
# NEW — crawfish/refine.py
class StopCondition(ABC):                       # external signal only
    @abstractmethod
    def satisfied(self, output: Output, ctx: RunContext) -> bool: ...
    @abstractmethod
    def progress(self, output: Output) -> float:   # ranking function in [0,1]

class RubricThreshold(StopCondition): ...       # rubric.score(output)[metric] >= at_least (metrics.py:468)
class PredicateStop(StopCondition): ...         # pure typed predicate
class VerifierStop(StopCondition): ...          # delegates to a gated Verifier (CL-2)

class Refine(Node):                             # kind = "refine" (NEW NodeKind, core/types.py:63)
    def __init__(self, body: Definition, until: StopCondition, *,
                 max_iters: int,                # hard bound — NEVER wall-clock
                 feedback_key: str = "_refine_feedback",   # prior attempt fed as FLUID
                 no_progress_patience: int = 1,
                 on_stuck: Literal["abstain","escalate","return_best"] = "return_best",
                 resume: bool = False): ...

    async def execute(self, inputs, ctx, rt) -> Output:
        # for i in range(max_iters):
        #   ctx.cancel_token.raise_if_cancelled()                # context.py:69
        #   if ctx.cost_budget.remaining_usd <= 0.0: break       # preflight, run.py:323
        #   feed best as FLUID feedback_key (taint propagates; never instructions)
        #   out = await Run(body, it_inputs).execute(ctx, rt)    # charges SHARED budget
        #   self._checkpoint(ctx, loop_id, i, out)               # F-2 ledger (no-op stub pre-CL-4)
        #   if until.satisfied(out, ctx): return out (+coordinate, refine_stopped="goal")
        #   noise-aware no-progress: stop if delta within calibrate rubric_std band
        # return on_stuck(best)   refine_stopped="exhausted"
```

A convenience alias `feature_loop(body, until=..., max_iters=...)` matches the vision vocabulary.

**Fixes applied:**
- *CL-1 AC#3 (shipping minor):* the preflight `remaining_usd <= 0.0` does not guarantee the *next* call fits. Softened AC to "stops once `remaining_usd <= 0` without exceeding the cap by more than one worst-case call," and noted the coupling to the cost-interval issue (OPT-2) for a next-call-fits preflight.
- *Noise-aware no-progress (ML missing; SECURITY major on C2):* the no-progress detector compares the `progress()` delta against the per-step `rubric_std` from `cw.calibrate` (F-8) — *not* byte-identical sha — so the loop neither stops on a noisy dip nor chases noise. This is the **one** no-progress vocabulary shared with C2/C3 (security major fix: reconcile CL-1's ranking-function and C2's sha-equality into this single calibrated detector).
- *`recurse` is not a separate operator at this layer:* bounded recursion is `Refine`/`Program` over a self-summoning body (resolved fully in C3).

**Determinism constraints.** One stochastic leaf per iteration (`Run.execute`); loop counter, stop check, no-progress, best-tracking are pure. **One shared `CostBudget`** threaded into every inner `Run` (never a fresh `RunContext`); preflight every call. Bounded by `max_iters` + budget + cancel + no-progress — never wall-clock (`tuner.py:26-27`). Feedback is **FLUID** (taint propagates, never instructions, `run.py:328-329`). Mutates nothing — produces new frozen Outputs (`Output.derive()`, F-0); `body` stays frozen (eval mode) throughout. Each iteration records its `iter_index` coordinate (F-1).

**Acceptance criteria.** (1) Improving scripted `MockRuntime` outputs stop on the first iteration the rubric clears the threshold; `refine_iters` equals that count. (2) Never-clearing outputs run exactly `max_iters`, return best-progress, `refine_stopped=="exhausted"`. (3) Budget sized for 2 iterations ⇒ exactly 2 metered calls, `spent_usd > 0.0` (Gap #3 closed), no `BudgetExceeded` mid-call beyond the one-worst-case-call bound. (4) Cancel stops cooperatively before the next call. (5) Flat `progress()` within the calibrated noise band stops after `no_progress_patience`. (6) Feedback appears as a FLUID parameter; taint-conformance confirms it never reaches an instruction slot. (7) Same cassette ⇒ identical iteration count and returned `Output` sha across runs.

**Dependencies.** F-0, F-1, F-8; `VerifierStop` → CL-2; durable `_checkpoint` → CL-4 (no-op stub until then, so CL-1 ships standalone). **Effort.** L. **Determinism risk.** Faithful-proxy: the `Rubric` must proxy the true goal; mitigated by `VerifierStop` (CL-2) being eval-gated.

### ISSUE CL-2 — `Verifier`: the gated external-signal critic (NEW)

**Problem.** `Refine`'s safety rests on an external stop signal; a free-running critic that can block a loop is as consequential as a `Sink`. A critic must **earn** the right to gate.

**API design (NEW).** Wraps a critic `Definition` (own version, knobs, `Rubric`); exposes `verdict(output) -> label` over a closed label set with a mandatory `default` (mirrors `Router`/`Classifier`, `router.py:190`). A class method `gated(...)` admits it as a `VerifierStop` source only after clearing an **absolute precision** bar against a decision `GoldenSet`.

**Fix applied (PL/SECURITY BLOCKER — the most important safety fix in the epic):** The draft called `gate_against_baseline(..., tolerance=1.0-min_precision)`. This is semantically wrong: `gate_against_baseline` computes relative *regression* against a stored baseline, and with **no baseline stored it returns `True` unconditionally** (`eval.py:251-252`) — so a never-benchmarked critic would be admitted to **block** production, the exact safety property inverted. **Resolution (via F-3):** `gated()` computes the critic's **absolute precision** directly against the decision `GoldenSet` (`TP/(TP+FP)`) and admits **only if** `precision >= min_precision` **AND a baseline exists**. The no-baseline case **fails closed** (`raise VerifierNotGated`). Regression-protection, if wanted, is layered as a *separate* gate call. A shadow→warn→block lifecycle: below the bar the critic stays in `warn`/`shadow` and cannot block.

**Determinism constraints.** The critic call is a leaf `Run` (replays via cassette); the label parse and precision computation are pure. Closed label set + mandatory `default` (unparseable ⇒ `default`, never a silent pass). Critic output reaching the body is FLUID. Critic `Definition` is frozen (content-hashed `export()`, `definition/types.py:132`).

**Acceptance criteria.** (1) A sub-`min_precision` critic raises `VerifierNotGated`; at/above it returns a usable `Verifier`; **no decision corpus ⇒ raises (fail-closed).** (2) `verdict` returns only declared labels; unparseable ⇒ `default`. (3) In a `Refine`, a gated verdict of `accept_label` stops; otherwise feeds forward as FLUID. (4) Each verifier call charges the shared budget (a second emission per iteration, `escalate.py:14`). (5) Frozen critic + cassette ⇒ identical verdict sequence.

**Dependencies.** F-3 (precision gate), F-4 (decision `GoldenSet` source; until then accept an authored set). Consumed by CL-1. **Effort.** L. **Risk.** Reward-hacking over many iters; mitigate by capping `max_iters`, surfacing per-iteration verdicts in the ledger, and periodic re-gate against fresh corrections.

### ISSUE CL-4 — Durable, crash-resumable `Refine` loops (NEW)

**Problem.** Crawfish owns the two halves Temporal separates — deterministic cassette replay (`runtime/replay.py`) and an append-only `ExecutionLedger` (`ledger.py`) — but they are not wired into a loop. A loop that crashes at iteration 3 of 5 restarts from scratch and re-pays.

**API design.** Implement `Refine._checkpoint` (the CL-1 stub) against the **F-2 composite-key ledger**, persisting each iteration's frozen `Output` (the `Workflow._save_state` pattern, `workflow.py:92`). Add `resume: bool` mirroring `Workflow.run(resume=...)` (`workflow.py:113`). On resume, completed iterations are loaded (not re-run); because the inner `Run` would replay from the cassette anyway (F-1 key incl. `iter_index`), determinism is **content-hash-verified**, not trusted.

**Fixes applied:** uses the F-2 schema (not "reuse verbatim" of the int-step ledger); `loop_id` is deterministic per F-2 (forbid `new_id()`); cassette-absence on resume re-runs and re-charges (fail-safe toward correctness). **Atomic checkpoint over (body output + verifier verdict)** (security missing): an iteration's checkpoint is atomic so a crash between the body and verifier calls never double-charges or skips the verifier.

**Determinism constraints.** Each iteration checkpoints with `org_id`; `raise_if_cancelled` before each step; resume produces a bit-identical continuation; persisted iteration outputs are frozen `Output` records by reference, never mutable channels.

**Acceptance criteria.** (1) Crash after iteration 2 of 5, then `resume=True` ⇒ **zero** new metered calls for iterations 0–2, resume at 3. (2) Resumed final `Output` sha identical to an uninterrupted run. (3) `completed_steps(loop_id)` reflects exactly the finished iterations. (4) State carries `org_id`; cross-org isolation. (5) Cancel between iterations leaves a clean, resumable checkpoint.

**Dependencies.** CL-1, F-2, `RecordReplayRuntime`. **Effort.** M. **Risk.** Coordinate the checkpoint key scheme with C2 (same substrate generalizes from loop iteration to back-edge traversal).

---

## Milestone 2 — Composition Surface: runnable branch, cyclic `Program`, bounded `recurse`

> Closes audit Gap #2 (the structural root) and the recursion facet. The back-edge that turns "pipeline" into "program/app." **Reviewer note:** `Program` was specced twice (C2, R1); **C2 is the runtime construct, R1 is its content-hashing + diff/merge surface — they compose.**

### ISSUE C1 — Make `Router` a runnable composition step (`branch`) (NEW)

**Problem.** `Router` is assembly-checked for totality (`UnroutableLabelError`, `router.py:185-194`) but `Workflow._run_step` raises `TypeError` on `NodeKind.ROUTER` (`workflow.py:187`). Branching is a helper returning `(label, Node)`, forcing hand-rolled dispatch that loses budget/taint/checkpoint guarantees (Gap #3).

**API design.** Add a `ROUTER` arm to `_run_step` that classifies each item and dispatches it through the *same* `_run_step` machinery (so a branch may be a `Sink`/`Batch`/`Filter`/`Aggregator`/`Program`). A thin constructor `branch(classifier, branches, *, name)`. Extend `check_types` (`workflow.py:68-90`): type-check the producer's output against **every** branch's input; the branches' (consistent) output becomes the next producer schema, else `WireError`.

```python
if isinstance(step, Router):
    routed = []
    for item in current:
        label, br = await step.route_async(item, ctx, rt) if rt else step.route(item)
        out = await self._run_step(br, [item], ctx, rt)
        routed.extend(o.model_copy(update={"lineage": item.lineage}) for o in out)  # taint carries
    return routed
```

**Determinism constraints.** Predicate `Classifier` is pure (zero model calls); `Definition`-backed `Classifier` is one leaf `Run` (`router.py:146`). Taint/lineage propagate via `Output.model_copy` (`workflow.py:176`); a tainted item routed to a static-only `Sink` still raises. Classifier `Run` charges the budget; `raise_if_cancelled` per item.

**Acceptance criteria.** End-to-end `Router` step (no `TypeError`); predicate routing with `spent == 0`; `check_types` raises `WireError` at assembly when a branch can't accept the input; an uncovered label still fails at construction (`UnroutableLabelError`); a tainted item to a static-only `Sink` still raises with taint preserved; replay yields identical label sequence. **Open (resolved):** all branches must converge to a compatible output schema unless the Router is *terminal* (every branch a `Sink`), which skips the convergence check. Recursion into the same Router is forbidden in a `Workflow` (cycles only in a `Program`).

**Dependencies.** None hard. **Effort.** S. (Lands on `main` as a quick win; unblocks the `ap-clerk` review Router.)

### ISSUE C2 — `Program`: cyclic-capable surface with content-addressed back-edges (NEW)

**Problem.** `Workflow` is forward-only; `BatchExecutor` rejects cycles (`executor.py:85-87`). Branch-then-recurse and guarded loops are unrepresentable. This is the structural keystone the durable `Refine` and `recurse` compose onto.

**API design.** A typed directed graph where **edges may cycle**, but every back-edge is a content-addressed version transition guarded by a deterministic predicate + bound. Reuses the `Workflow` kernel (`_run_step`, `_save_state`, the F-2 ledger); the difference is the *driver* (walk edges, not `for step in steps`).

```python
app = cw.Program(name="ap-clerk", version="2.0")
extract = app.step(refine_extract)          # may be feature_loop / Batch / branch
review  = app.step(review)                  # a runnable Router (C1)
app.edge(review, extract,
         when=lambda label, out: label == "mismatch",   # pure predicate
         max_visits=3, budget=cw.CostBudget(limit_usd=0.40), on_stuck="dead_letter")
```

Execution: per-item walk; a back-edge whose `when(...)` holds (1) `derive()`s the current `Output` to a new immutable value and records its `output_content_sha` (F-0) as the ledger key; (2) increments a per-`(item_id, edge_id)` `visit_count`; (3) checkpoints to the F-2 ledger. Bounding halts on `visit_count >= max_visits` OR shared-budget exhaustion (preflight `remaining_usd`) OR cancel OR **calibrated no-progress** (see fix). Durable resume replays completed iterations from cassette at $0. Assembly checks: every edge (incl. back) type-checks via `parameters_compatible`; a back-edge requires the target accept the source's output; **an unbounded back-edge raises `UnboundedCycleError` at assembly**; reachability holds.

**Fixes applied:**
- *No-progress (SECURITY major):* byte-identical-sha no-progress is too weak live (stochastic leaves rarely repeat bytes) and too aggressive on replay (identical cassette ⇒ premature stop). **Replaced** with the CL-1 **calibrated ranking-function** detector (delta within `rubric_std` from `cw.calibrate`) — one shared vocabulary across CL-1/C2/C3.
- *`Output` freeze (SECURITY major):* `Output` is already frozen (`output.py:44`); reuse `Output.derive()` + `output_content_sha` (F-0) for the ledger key. No second freezing path. The "make `Output` Freezable vs hash `model_dump`" dichotomy is removed.
- *Ledger (PL major):* uses the F-2 composite key, flagged NEW.
- *Per-item back-edge:* a `Batch` node fans out per item; the back-edge is per source item (counters keyed `(item_id, edge_id)`).
- *Run identity:* the ledger records content-hash **references**, not embedded Outputs (vision §5 open Q resolved toward reference-by-version).

**Determinism constraints.** Each back-edge mints a new content sha (no in-place edit; `Freezable.__setattr__`, `version.py:59-62`). One shared `CostBudget`, preflight + hard-kill (`context.py:42-47`). Checkpoint + cancel each iteration. Taint carries across the cycle (`output.py:57-65`). Every ledger row carries `org_id`. Same `AgentRuntime` seam (no SDK import).

**Acceptance criteria.** Back-edge runs to fixed point / `max_visits` / budget / cancel then takes `on_stuck`; `CycleError` not triggered (the `Program` driver owns cycles). Unbounded back-edge ⇒ `UnboundedCycleError` at assembly. Bad back-edge schema ⇒ `WireError` at assembly. Budget hard-stops mid-iteration; `spent` reflects every iteration (no Gap #3 leak). Calibrated no-progress ⇒ `on_stuck` dead-letter. Replay ⇒ identical version sequence + final Outputs, bit-for-bit. Durable resume at iteration *k* re-derives `1..k-1` at $0. Each iteration appends a distinct `output_sha`; rows carry correct `org_id`.

**Fix applied (shipping major — split the XL):** C2 splits into **C2a** (Program driver + cyclic `check_types` + `UnboundedCycleError` — the spine) and **C2b** (per-iteration ledger versioning + durable resume), so neither ticket is an unplannable XL.

**Dependencies.** C1, F-0, F-1, F-2, F-8 (no-progress band). **Effort.** XL → split into C2a (L) + C2b (M).

### ISSUE C3 — `recurse`: bounded self-referential `Definition` invocation (NEW)

**Problem.** Recursion is partial: `AgentSpec.delegates_to`/`dependencies` (`definition/types.py:40-62`) give delegation with unbounded backend depth, not a Crawfish-owned guarded construct.

**API design.** Resolves the vision §5 open question: **`recurse` is a depth-guarded `Program` back-edge re-entering the same `Definition`, pushing a frozen version onto a per-item depth stack.** Reuses C2's kernel; the only deltas are a **depth bound** (distinct from `max_visits`) and a **base-case predicate**.

```python
plan = cw.recurse(planner,
    base_case=lambda out: out.value["is_atomic"],     # pure predicate
    max_depth=5,                                       # hard guard (≠ max_visits)
    budget=cw.CostBudget(limit_usd=1.00), combine=cw.collect, on_stuck="dead_letter")
```

Descent `derive()`s + pushes a new content sha; the base-case predicate (pure) stops descent; `combine` (existing `collect`/`count`/`dedupe`) folds children. Halts on `base_case` / `depth >= max_depth` / budget / cancel / calibrated no-progress. `max_depth` is assembly-required (no `max_depth` ⇒ `UnboundedRecursionError`).

**Determinism constraints.** Every descent mints a new sha (version stack, no in-place mutation). Whole-tree shared budget, preflight each descent, hard-kill on breach. `max_depth` + base-case are the termination argument — never wall-clock. Taint carries down and up; a tainted sub-result stays tainted through `combine`. Each level checkpoints (F-2 depth variant). `combine` is order-deterministic (depth-first, descent order).

**Acceptance criteria.** Base-case at depth `d ≤ max_depth` ⇒ `combine` folds exactly the children. Never-base-case ⇒ halts at `max_depth`, `on_stuck`; never exceeds `max_depth` calls per path. No `max_depth` ⇒ `UnboundedRecursionError` at assembly. Budget hard-stops; `spent` reflects every level. Replay ⇒ identical descent/combine sequence + folded Output. Resume at depth *k* replays `1..k-1` at $0. Each level appends a distinct sha; rows carry `org_id`.

**Fix applied (security/aggregate missing):** `combine` taint rule made explicit and added to the ALG-7 conformance suite — **the reduced Output is tainted if ANY child input was tainted** (taint = union, mirroring `run.py:261`). "A vote/fold does not launder taint."

**Dependencies.** C2 (cyclic kernel + version-stack ledger), F-0, F-8. **Effort.** L. **Risk.** Tree fan-out `O(b^d)`; the shared budget is the real guard, and the cost estimator (OPT-2) folds `b^max_depth × per-iter`.

---

## Milestone 3 — Tunable ML Library: train/eval, state dicts, calibration, gates, objectives, explore

> The unifier and the statistically-honest optimization surface. **Reviewer dedup:** `calibrate` (AL-T4/TS-2), `state_dict` (AL-T2/AL-DV5), `Objective` (AL-T3/TS-6) were each specced twice — merged below. Every gate consumer inherits F-3 + F-8.

### ISSUE AL-T1 — The two-axis mode unifier: per-knob `tunable` + `train()`/`eval()` (NEW)

**Problem.** PyTorch's hardest lesson: `requires_grad=False` and `.eval()` are different axes. Crawfish's `Freezable.frozen` is the eval lock, but the Tuner's "which knobs may move" mask is imperative code at the call site (`tuner.py:223`), not a property of the artifact.

**API design.** Axis 1 — `tunable` per knob as data (`TuneSpec` content-hashed into `Definition.tune`, authored as `tune.toml`):

```python
class KnobDomain(BaseModel): path: str; values: list[JSONValue]; tunable: bool = True
class TuneSpec(BaseModel):   knobs: list[KnobDomain] = []
def named_knobs(self) -> Iterator[tuple[str, KnobDomain]]: ...   # only tunable=True, path-sorted
```

Axis 2 — `train()`/`eval()` scope (eval is default): `train(defn)` returns an **unfrozen** copy (the `_with_agents` path, `tuner.py:119-122`); `eval(defn)` is `_refreeze` (`tuner.py:102`). **Load-bearing rule:** a consequential Sink may fire, a run may be recorded, and a content hash is stable **only in eval mode**; a Sink against an unfrozen Definition raises (extends `StaticOnlyError`/`FrozenError`).

**Fix applied (F-5):** `temperature`/`top_p`/`sample_k` become first-class tunable `AgentSpec` fields (enter the content hash); the `RunRequest` value is **derived**, never independently set. Decode-knob ownership is settled in the F-5 ADR *before* this ships. The content-hash bump migration is documented (F-5).

**Determinism constraints.** `train()`/`eval()` only ever produce a new frozen sha via `_refreeze`; `TuneSpec` is static config in the content hash; mode adds zero stochasticity; Sink-in-eval-only strengthens the thesis.

**Acceptance criteria.** `tune` round-trips through `export()` and changes the sha when edited. `train(d).frozen is False` with a fresh `Version`; `eval(train(d))` re-hashes to `d`'s eval sha (idempotent). A `TuneSpec`-driven mutator proposes the same set as the hand-built one and **refuses** to mutate a `tunable=False` knob. A Sink against an unfrozen Definition raises; against eval-mode succeeds. `named_knobs()` yields only tunable paths, sorted.

**Dependencies.** F-5. **Effort.** M.

### ISSUE AL-T2 — `state_dict()`/`load_state()`: content-hashed, references-by-version transfer (NEW)

**Problem.** Tuner/LearningLoop outputs are opaque whole-Definition blobs (`learning.py:70`). No architecture/weights split ⇒ no transfer, no fleet sharing, no A/B on one shape.

**API design.** `StateDict` = tunable knobs only (per-role prompt, `injected_prompts`/few-shots, `model`, `temperature`/`sample_k`, `context_strategy`, `policies`, `coordination`) + **summoned units as `DefinitionRef` references-by-version** (`definition/types.py:69`) — never the architecture (team topology, IO schema, dependencies). JSON only (DSPy stance — loading never executes code). `load_state(strict=, only=)` is **copy-on-write** via `_refreeze` (new sha, never in place).

**Fix applied (vision §5 open Q):** summoned units stored by pinned version reference, not embed — keeps the hash bounded and replay reproducible.

**Determinism constraints.** Knobs-only, content-hashed; load mints a new sha; only STATIC knobs move (no fluid crosses, `learning.py:33-34`); JSON-only; references-by-version.

**Acceptance criteria.** `state_dict()` excludes architecture keys; editing a knob changes `StateDict.sha`. `d.load_state(d.state_dict())` is sha-identity. `strict=True` raises on a shape mismatch; `strict=False` loads the intersection. `only=["fewshots"]` transfers only few-shots. Summons are `{id,version}` refs (embedding a full nested Definition is rejected at validation); `export().checksum` reflects pinned summon versions. A loaded state replays bit-for-bit in eval mode (F-1 key).

**Dependencies.** AL-T1 (`TuneSpec` defines the knob membership — single authoritative declaration shared with the Tuner to avoid drift), F-5. **Effort.** M.

### ISSUE AL-T4/TS-2 (merged) — `cw.calibrate()`: variance / calibration / abstention (NEW)

**Problem.** `Benchmark.run` does one `Run` per task and means the rubric — run-to-run variance is structurally invisible (`metrics.py:508-518`), and `EscalatingRuntime`'s threshold (`escalate.py:56`) is a guessed constant with no evidence confidence predicts correctness.

**API design.** `cw.calibrate(definition, golden, *, runs, ctx, runtime) -> CalibrationReport` runs each case N times under **distinct seeds derived from one recorded base seed** (`random.Random(f"{seed}:{i}")`, the `FewShotMutator` discipline, `tuner.py:325`), under a **non-replay** runtime (the one legitimate exception — replay would zero variance). Reports `rubric_mean`/`rubric_std`, `output_variance` (via `structural_diff`, `metrics.py:393`), calibration metrics, `abstention_rate`. Content-hashed, `org_id`-tagged.

**Fixes applied (ML major):**
- **Primary calibration metric is Brier/NLL** (unbiased, no binning); **ECE is a diagnostic** computed with **adaptive / equal-mass binning** and a **bootstrap CI**. **Gating is forbidden when a calibration metric's CI is wider than the gate margin** (F-8 anytime-valid discipline) — so a high-variance point estimate cannot reintroduce noise-promotion.
- **Records the `AgentRuntime` determinism capability tier** (F-5) and attributes a **variance floor to infra** separately, so model-stochasticity is not conflated with backend nondeterminism.
- **Refuses a `RecordReplayRuntime`** with a clear error (not a silent variance=0).
- N runs charge N× through the shared budget; bounded by `runs × len(golden)` + budget + cancel; a ceiling breach returns a partial report (the Tuner's ceiling-returns-base analogue, `tuner.py:523`).

**Acceptance criteria.** Under a seeded-varied runtime, non-zero `rubric_std`/`output_variance`; under a fully deterministic runtime, `output_variance == 0`. Same `(seed, runs)` ⇒ identical per-run seed schedule (procedure reproducible). Brier is computed; ECE is `None` without labels, else in `[0,1]` with a CI; a perfectly-calibrated synthetic fixture yields Brier consistent with label noise and an ECE-CI covering ~0. `CalibrationReport` is frozen, carries `org_id`, records the determinism tier. Calibrate raises on a replay runtime.

**Dependencies.** F-3, F-5, F-8; `GoldenSet`/`Benchmark`/`Rubric`; confidence extraction (`escalate.py:34-53`). Consumed by AL-T5, AL-T3, AL-T6, TS-4, CL-1/C2/C3 (no-progress band). **Effort.** L.

### ISSUE AL-T5 — Variance-aware promotion gate (NEW; extends `gate_against_baseline`)

**Problem.** The promotion gate accepts on a single benchmark run (`tuner.py:558-563`; `is_regression` is single-point, `metrics.py:532-546`) with a hand-set `tolerance` ⇒ over-promotes on noise.

**API design.** Store per-metric **std** alongside baseline scores (`save_baseline`/`load_baseline`, `eval.py:230-238`, gain a parallel `*_std` record). Promote only when the candidate beats baseline beyond the noise band. **Implemented entirely through F-3's gate algebra:** paired test over per-case deltas, family-wise correction across the Rubric vector, `k` derived from a stated α, Clopper-Pearson only for binary metrics. Backward-compatible: `std=0, k=0` reduces to today's behavior. Winner's-curse correction (F-8): re-estimate/shrink the promoted argmax's score on a fresh sample before storing it as the new baseline.

**Determinism constraints.** Pure arithmetic over recorded scores + recorded std; same inputs ⇒ same decision. The eval gate stays the safety net (`learning.py:22-34`); this only makes it noise-robust.

**Acceptance criteria.** `std=0,k=0` byte-identical to today. Baseline 0.80±0.05, candidate 0.82 (in band) ⇒ rejected; candidate 0.92 (beyond `k·std`) ⇒ accepted, using a **paired** delta test. A rich rubric does not inflate false-promotion past α after correction. Baseline std persists with `org_id` across restarts. Promoted baseline is winner's-curse-corrected. Same seed + recorded std ⇒ identical decision.

**Dependencies.** AL-T4/TS-2 (std), F-3, F-8. **Effort.** M.

### ISSUE AL-T3/TS-6 (merged) — `Objective`: cost-regularized loss (NEW)

**Problem.** The Tuner's acceptance is a pure-quality Pareto rule (`tuner.py:559-563`) with **no cost term** — it will promote a 1%-better, 5×-pricier candidate (cost only bounds budget, `tuner.py:553`).

**API design.** `Objective.value(scores, *, cost_usd, ece) = Σ wᵢ·scoreᵢ − λ·cost − μ·ece`. Per-candidate `cost_usd` from `estimate_cost` (deterministic). The Tuner gains an optional `objective`; when set, acceptance maximizes `Objective.value` **while keeping `is_regression` as the hard gate** — the cost term only **re-ranks among candidates that already pass the regression gate**, so it can never promote a quality regression.

**Fixes applied (ML minor):** **normalize the cost term** (relative to the cheapest candidate, or `$/quality-point`) so λ is unit-free and portable. Offer the **ε-constraint form** (minimize cost subject to `quality >= floor`) alongside the linear scalarization. `pareto=True` returns the non-dominated set; the gate requires non-domination. λ is a project-policy meta-knob (out of tree). `ece_weight` consumes AL-T4's CI-bounded calibration metric (ships as 0 until calibrate lands).

**Determinism constraints.** Pure arithmetic over recorded scores + deterministic cost; changes *which* candidate is accepted, never adds stochasticity; hard regression gate non-negotiable.

**Acceptance criteria.** `cost_weight=0` ⇒ same winner as today (back-compat). Two equal-quality candidates ⇒ the cheaper one promoted; a 2%-better/5×-pricier candidate rejected for a suitable λ. A candidate maximizing the objective but regressing past `-tolerance` still rejected by `is_regression`. ε-constraint form picks the cheapest candidate above a quality floor. `pareto=True` never promotes a dominated candidate. Same inputs ⇒ same scalar.

**Dependencies.** `compare`/`is_regression` (`metrics.py:522-546`), `estimate_cost` (OPT-2), the Tuner acceptance site, F-8. **Effort.** S/M.

### ISSUE AL-T6 — The explore-rate dial: bounded, anytime-valid, eval-gated online loop (NEW)

**Problem.** `LearningLoop.improve` is a one-shot offline optimizer (`learning.py:195`). No serving-time mechanism routes a bounded fraction of live items to a trial candidate and feeds outcomes back.

**API design.** A `ServingLoop`: route `(1-ε)` to the promoted best, `ε` to a trial candidate by **seeded hash of the recorded `item_id`** (so a replay re-explores exactly the same items — deterministic under replay). Captured outcomes (`capture_case`, `eval.py:56`) feed the `GoldenSet`; a trial graduates **only** through the gate. ε is bounded by the shared `CostBudget`.

**Fixes applied (ML BLOCKER):**
- Bare fixed-ε is the weakest bandit and a continuously-re-tested gate has an **optional-stopping / peeking** failure mode (inflated false-promotion). **Resolution:** graduation uses an **anytime-valid sequential test** (a confidence-sequence / mixture-SPRT, time-uniform bounds) OR a **pre-registered per-trial sample size** before the gate is consulted (F-8). "No peeking / pre-registered N or anytime-valid bound" is an acceptance criterion.
- Replace bare fixed-ε with at minimum a **decaying-ε schedule**, and add a typed `SearchStrategy` hook for **UCB1/Thompson** (these need only per-arm reward mean + count, already in the emission ledger, `tuner.py:_emit`). Explored-arm emissions are tagged `explore=True` so emission history can drive `craw code`'s policy.

**Determinism constraints.** Every stochastic choice (which items explored) derives from one recorded seed ⇒ identical explored set under replay ⇒ identical graduation. Promotion stays eval-gated and reversible (`learning.py:266`). Only STATIC knobs are ever promoted (`learning.py:33-34`).

**Acceptance criteria.** `explore_rate=0` ⇒ no exploration (no-op overlay). Same `(seed, item_ids)` ⇒ identical explored subset. A trial losing to baseline (under the anytime-valid test) never becomes active; the promoted best is unchanged. ε stops when the budget is exhausted (non-promoting outcome). All captured cases + promoted records carry `org_id`. Graduation controls Type-I error under continuous peeking.

**Dependencies.** AL-T4/TS-2, F-3, F-8, `LearningLoop`, `capture_case`, `CostBudget`. **Effort.** L. **Risk.** Non-stationary drift (when to raise/lower ε) is `craw code` policy, out of framework scope.

---

## Milestone 4 — Taming-Stochasticity Operators

> The deterministic envelope around the leaf: quorum, abstention, house-guard, constrained decoding. **Reviewer dedup:** `Refine`→CL-1, `calibrate`→AL-T4/TS-2, `Verifier`→CL-2, `Objective`→AL-T3/TS-6. Remaining here: TS-1, TS-4, TS-7, TS-8.

### ISSUE TS-1 — Typed quorum / self-consistency aggregator (`Quorum`) (NEW)

**Problem.** Self-consistency (sample N, take consensus) is the cheapest, best-attested variance reducer and the purest expression of the thesis — N stochastic leaves reduced by a deterministic vote — but the aggregator layer has only `collect/concat/count/dedupe`, and `fan_in` reduces *distinct* items, not *k samples of one item*.

**API design.** A `QuorumRuntime` wrapping any inner runtime, sampling the *same* request k times (each charges + emits through `inner`, the `escalate.py:99-109` pattern), with a typed consensus reducer.

```python
def majority_vote(*, field=None) -> ConsensusFn: ...   # canonicalizes via StructuralMatch/SetOverlap (metrics.py:258-400)
def rubric_argmax(rubric: Rubric) -> ConsensusFn: ...   # argmax; tie-break via a gated Verifier (CL-2), never the generator
```

`sample_k` is a tunable knob (AL-T1) so the Tuner searches the cheapest k that hits a reliability target, paired with the cost-regularized objective.

**Fixes applied:**
- *Cassette collision (SECURITY BLOCKER):* under the unsalted key, k samples collide into one cassette (unanimous no-op, variance=0). **Resolution:** uses the **F-1 `sample_index` coordinate** so k recorded samples are distinct cassettes; **until F-1 lands, `QuorumRuntime` refuses to run over a `RecordReplayRuntime`** (raises, like calibrate's replay-refusal) rather than silently returning a unanimous no-op.
- *Estimand + ill-defined plurality (ML major):* define the estimand — modal output (`majority_vote`) or argmax of expected rubric (`rubric_argmax`). For `majority_vote`, **mandatory canonicalization** collapses semantically-equal outputs; on high-cardinality outputs where plurality is ill-defined, **fall back to abstention** (TS-4).
- *Optional-stopping `early_stop` (ML major):* fixed `early_stop=0.8` has no statistical basis. **Replaced** with a **sequential proportion test** (stop when a Wilson lower bound on the lead exceeds 0.5, or after a pre-registered k) — F-8.
- *Winner's-curse on `rubric_argmax` (ML major):* the argmax over a noisy rubric is upward-biased. **Resolution:** report the runner-up gap and **abstain (TS-4) when the gap is within the rubric noise band** from `cw.calibrate`.
- *Aggregate taint (SECURITY missing):* the consensus winner is tainted if **any** sample was tainted (union); "a vote does not launder taint" — an ALG-7 conformance case.

**Determinism constraints.** Each sample is an isolated leaf; consensus is pure over recorded `RunResult.text`. Every sample charges the shared budget + preflights `remaining_usd`. Bounded by k + budget + cancel — never wall-clock.

**Acceptance criteria.** k=5 over `RecordReplayRuntime` returns the majority typed Output bit-for-bit; k distinct cassette keys (via the F-1 coordinate). Budget below 5× per-call ⇒ stops early, never exceeds the ceiling. Sequential-test early-stop returns after the lead is statistically real; same seed ⇒ identical sample count + winner. `majority_vote(field="label")` collapses `{"a":1,"b":2}` and `{"b":2,"a":1}` to one candidate; a high-cardinality input abstains. Cancel between samples honored. Winner tainted iff any sample tainted.

**Dependencies.** F-1, AL-T4 (noise band), CL-2 (tie-break/abstain), TS-4. **Effort.** M.

### ISSUE TS-4 — Abstention as a typed Output discipline (NEW)

**Problem.** Selective prediction (decline rather than hallucinate) is the formal frame for reliable agents, but there is no abstention primitive; `EscalatingRuntime` escalates but never gives up.

**API design.** An `Abstention` typed payload (`reason`, `confidence`, carries the producing run's taint) plus `abstain_below(threshold, *, field="confidence")` (mirrors `confidence_below`, `escalate.py:56-75`). `Abstention` is static-only-safe and routable: a `Router` can branch `Abstention → review_sink`. The threshold is **derived from `cw.calibrate`'s reliability curve** (the confidence where expected accuracy crosses target), never a naive constant.

**Determinism constraints.** Pure threshold over a recorded confidence; `Abstention` is frozen and carries taint; threshold derivation is pure arithmetic over the calibration report.

**Acceptance criteria.** A 0.5-confidence result under `abstain_below(0.7)` yields an `Abstention`. `Abstention` type-checks as a valid Output and is routable. A calibration-derived threshold differs from a naive constant on a mis-calibrated fixture. `calibrate`'s `abstention_rate` matches the policy's abstention share. An abstaining run still charges what it spent.

**Dependencies.** AL-T4/TS-2 (threshold), `nodes/router.py`. Composes with CL-2 (verifier-fail → abstain) and CL-1 (no-progress → abstain). **Effort.** S. **Risk.** Abstention is only as good as the calibration behind the threshold — a raw constant is unsound.

### ISSUE TS-7/R4 (merged) — House-guard: learned-then-distilled deterministic guards with a precision-AND-coverage certificate (NEW)

**Problem.** The deepest thesis expression: a program accretes its own deterministic invariants — learn stochastically, distill to a pure predicate, earn enforcement.

**API design.** (1) `from_corrections` (F-4) mines corrections into a `GoldenSet`. (2) The model **proposes** a rule — the one stochastic leaf. (3) **Distill** to a pure `Metric`/predicate returning 0/1 with zero model calls (the `Metric` ABC, `metrics.py:141`). (4) **Earn** enforcement only by clearing the gate. Lifecycle: shadow → warn → block, content-hashed, eval-gated, reversible (`VersionRecord` lineage).

**Fixes applied:**
- *Precision-alone is insufficient (ML minor):* the `GuardCertificate` reports **both** a precision lower bound **and** recall/coverage with CIs; graduation gates on a **joint** criterion (`precision_floor AND min_coverage`) — a 99%-precision / 2%-coverage rule cannot block. Adds a **periodic re-gate against fresh corrections** with a staleness bound and an enforcement-rate **drift alarm** (distribution shift).
- *Predicate grammar (SECURITY minor):* the distilled predicate is a **fixed, total, side-effect-free expression grammar** over typed Output fields (comparisons, set membership, numeric bounds) evaluated by an interpreter — **never `eval`/`exec`**. The proposer output is FLUID and can never widen the grammar; the distilled AST becomes STATIC only after the precision gate.
- Statistical machinery (precision LB, coverage CI) comes from F-3/F-8; the precision gate **fails closed** (no corpus ⇒ stays in `warn`).

**Determinism constraints.** Proposal is the isolated leaf; the distilled predicate is pure (same input ⇒ same 0/1, zero model calls, replays identically); enforcement is eval-gated + reversible; each synthesized validation mints a new content sha (never edits a frozen prior rule).

**Acceptance criteria.** `from_corrections` builds a `GoldenSet` matching the ledger count. The distilled predicate is pure ($0, no model call). A guard below its joint floor stays in `warn` (cannot block a Sink); clearing the joint floor reaches `block`. The certificate reports precision-LB **and** coverage with CIs, honestly. A promoted guard rolls back reversibly. Re-running synthesis on a fresh corpus mints a new sha. The predicate grammar is closed; FLUID corrections cannot widen it.

**Dependencies.** F-3, F-4, F-8; `Metric` ABC; emission ledger; shares `from_corrections` with CL-2. **Effort.** L.

### ISSUE TS-8 — Constrained / grammar-guided decoding as a runtime-call property (NEW)

**Problem.** Crawfish does only post-hoc validate+repair (`run.py:298-337`). Decode-time constraint is strictly stronger — it eliminates the parse-failure path (and its metered repair call) for backends that support it.

**API design.** Optional `grammar`/`decode_seed` on `RunRequest` (per F-5: `grammar` is a per-call property kept out of the content hash; `temperature` is *derived* from the resolved Definition, not set here). When the resolved Definition declares an output schema and the runtime advertises grammar support, the schema is passed as `grammar` and the repair path becomes dead code; where unsupported, behavior is unchanged (graceful degradation).

**Fixes applied:**
- *Run identity (SECURITY major):* any decode field that affects output **must** enter run identity — `decode_seed` enters the **F-1 `_key` extension**; `temperature` enters `version.sha` (F-5). "Extend `_key` to include any new decode-control field" is an acceptance criterion. No decode field sits outside run identity.
- *Reasoning opt-out (the "Let Me Speak Freely?" caveat):* a reasoning-heavy step may set `grammar=None` even when a schema exists.

**Determinism constraints.** Decoding control is a property of the leaf, not new control flow; it *strengthens* determinism (moves malformed output from a retried failure to an impossible state). `decode_seed` recorded into run identity ⇒ bit-for-bit replay where the backend honors it (per the F-5 capability tier).

**Acceptance criteria.** A grammar-supporting runtime never enters `_repair` (repair counter 0 where previously >0). A non-supporting runtime degrades to validate+repair with no behavior change. `decode_seed`/derived `temperature` are threaded + recorded; same seed ⇒ same recorded result. A schema-bearing step can opt out with `grammar=None`. A constrained call shows lower recorded cost (no repair call). Two different decode settings do **not** collide on one cassette (F-1).

**Dependencies.** F-1, F-5. Independent otherwise — can ship early. **Effort.** M (S for the field additions).

---

## Milestone 5 — Surfaces & Accuracy: CLI, cost honesty, live caching, dependency lockfile

> The surface of the train/eval engine and the honesty of its numbers.

### ISSUE OPT-1 — `craw eval / tune / refine / learn / guard`: the optimization-plane CLI (NEW)

**Problem.** The audit's sharpest surface gap: "the entire optimization plane is libraries with no CLI" (`§3`). `craw code` drives Crawfish through the shell (the `craw manage` model, `cli.py:431-448`), not by importing the SDK. Without these, the self-optimizing app is unbuildable by an agent.

**API design.** Five `_cmd_*` subcommands wired in `_build_parser`, each with `--budget` (→ `CostBudget` via `Budget.as_cost_budget`, `cost.py:185`), `--seed`, `--org`, and a **mandatory `--json`** machine-readable mode (versioned schema from day one — `craw code` parses it). `craw eval` drives `gate_against_baseline` (exits non-zero on regression). `craw tune` drives `Tuner.tune` (reuses `TuneResult.stopped_reason ∈ {exhausted,budget,cancelled,max_trials}`, `tuner.py:388`). `craw refine` drives the CL-1 operator (`--until 'rubric>=0.95' --max-iters 4`). `craw learn` drives `LearningLoop.improve` / `--rollback <sha>` (a pointer move). `craw guard` drives TS-7/R4 synthesis (shadow|warn|block). Optimization commands run in **train** mode; everything else in **eval** mode; an eval-mode run against an unfrozen Definition is a hard error.

**Determinism constraints.** Pure orchestration; all randomness flows through the recorded `--seed`. The `--json` surface never lets a Sink fire (Sinks are eval-only). `--org` threads `org_id` to every Store read.

**Acceptance criteria.** All five register and accept `--budget --seed --org --json`. `craw tune --seed S` twice ⇒ byte-identical `TuneResult.winner` sha + trial log. `craw eval` exits non-zero iff a regression; `--json` emits per-case deltas. `craw tune --budget 0.50` stops with `stopped_reason="budget"`. `craw learn --rollback <sha>` re-activates a prior `VersionRecord` (no model call). Every `--json` schema is snapshot-tested.

**Dependencies.** `craw refine` → CL-1; `craw guard` → TS-7/R4. `eval/tune/learn` depend only on existing libraries. **Effort.** L. **Open (resolved):** `craw refine` and the `Refine` operator share one `--until` expression DSL over Rubric metrics.

### ISSUE OPT-2 — Cost model that sees escalation/repair/retry/refine (the honest interval) (NEW; single owner of `cost.py`)

**Problem.** `estimate_cost` is a silent lower bound ("one run per agent per item," `cost.py:103`), blind to escalation's 2× tail (`escalate.py:99-109`), repair's +1 (`run.py:333`), retry's ≤N, and `Refine`'s `max_iters`. Closes Gaps #5/#9.

**API design (the F-6 canonical cost issue — CL-3 and ALG-5 are *consumers*).** Extend `CostEstimate` with additive `expected_usd`/`worst_case_usd` (existing `total_usd` unchanged = the lower bound). A `CostShape` describes the cost-bearing wrappers; the **composition law is multiplicative along operator nesting** (F-6): `worst_case = lower × Π(refine max_iters × escalate 2 × quorum k × retry n × recurse b^max_depth)`, escalation re-priced on `strong_model`. `expected` uses measured rates (from `cw.calibrate`/ledger) **as a CI-aware band**, never a falsely-precise point; with no rates, `expected == worst_case`.

**Fixes applied:**
- *Three issues editing `cost.py` (shipping major):* collapsed to this one owner; CL-3/ALG-5 consume it. `total_usd` unchanged is a hard AC (no breaking `craw dev --estimate`).
- *Nested composition (SECURITY minor):* a `Refine(4)` over `Escalating(2×)` over `Quorum(5)` previews `4×2×5 = 40×`; a `recurse` adds `b^depth` — a nested-operator AC asserts the multiplicative bound.
- *Falsely-precise expected (ML missing):* `expected` is a CI-aware band; rates carry uncertainty.

**Determinism constraints.** Pure static analysis through the *same resolver the runtime uses* (`route_decision`/`resolve_model`, `cost.py:127-131`) so the preview can't drift; rates read from the deterministic ledger, never sampled live during estimation.

**Acceptance criteria.** `CostShape(escalation=True)` ⇒ `worst_case` priced on `strong_model`, ≥ `total_usd`. `refine_max_iters=4` ⇒ `worst_case == 4 × lower`. A nested `Refine∘Escalate∘Quorum` asserts the 40× product. Measured `escalation_rate=0.2` ⇒ `expected` strictly between lower and worst-case, with a CI. No rates ⇒ `expected == worst_case` (no undercount). `craw dev --estimate` prints the three-number interval; `--json` includes all three. Same Definition + shape ⇒ identical numbers, no model call.

**Dependencies.** `Refine` (CL-1) for the `refine_max_iters` multiplier (ship escalate/repair/retry first); `cw.calibrate` for measured rates (degrades to worst-case). **Effort.** M. **Open (resolved):** a `CostShape.from_runtime(runtime)` helper infers the shape from the assembled wrapper chain (optional, cleaner for `craw code`).

### ISSUE OPT-3 — Live single-flight caching (NEW; extends `CachingRuntime`)

**Problem.** `CachingRuntime` only hits a *pre-recorded* cassette (`cache.py:97`, `_is_hit` checks `path.exists()`). Two identical items in one `Batch` both miss and both spend.

**API design.** An in-process per-key `asyncio.Future` map. The first caller computes; concurrent identical callers await the same in-flight result. The key is unchanged (`_key`, plus the F-1 coordinate where applicable) — single-flight is a strict refinement.

```python
existing = self._inflight.get(key)
if existing is not None:
    self.stats.coalesced += 1; self.stats.saved_usd += self._costs.get(key, 0.0)
    return await existing                           # one model call serves N callers
# else: create future, run inner ONCE (one CostBudget.charge), set_result, finally pop
```

**Determinism constraints.** Deduplication of identical deterministic-keyed calls — cannot change the result, only how many times the leaf runs. **Exactly one `inner.run` per key ⇒ one `CostBudget.charge`** (strengthens the gas meter). In-process only (no cross-process locking surface). The miss path still writes the cassette, so the next process hits it.

**Acceptance criteria.** Two concurrent identical calls (no prior cassette) ⇒ exactly **one** `inner.run`. `stats.coalesced == 1`, `misses == 1`, `saved_usd` == the avoided second spend. Budget charged exactly once for the pair. Cancel raises before coalescing. An in-flight exception propagates to all awaiters and the key is cleared (no poisoned future). Replay is bit-for-bit whether coalesced or not (one cassette).

**Dependencies.** None (extends `CachingRuntime`). **Effort.** M. **Open (resolved):** cross-worker coalescing (Store-backed lock) is explicitly out of scope.

### ISSUE OPT-4 — Dependency resolver + lockfile for summoned units (NEW)

**Problem.** `craw freeze` (`cli.py:133`) only hashes already-discovered local units (`cli.py:138-148`) — no resolution. `DefinitionRef` + `Registry` exist (`definition/types.py:69`, `discovery.py:53`) but nothing solves a version range, detects conflicts, or produces a pinned transitive closure. As Definitions summon units, an unpinned closure breaks replay reproducibility.

**API design.** A pure, deterministic `resolve(root, registry, *, constraints) -> Lockfile` walking `root.dependencies` transitively, matching each `DefinitionRef.version` (exact pin or `^`/`~` range) against registry candidates, picking the highest compatible, detecting conflicts, pinning to `(version, content-sha)`. `Lockfile.closure_sha()` is one hash over the sorted pin set — a run embeds this **reference**, keeping run identity small (vision §5 reference-by-version). `craw freeze` writes the transitive closure; `craw freeze --check` verifies on-disk drift (CI gate). Pure-Python SemVer comparator (no new third-party dep, ADR discipline).

**Fixes applied (shipping/PL minor):** `DefinitionStore` (AL-DV2) vs `Registry` reconciliation — **Registry stays discovery-only** (immutable entry points + dir scan); `DefinitionStore` owns mutable name pointers; `resolve` uses content-addressed units (org-agnostic), the recorded closure ref carries `org_id`.

**Determinism constraints.** Resolution is pure and offline (no network, no model call); deterministic ordering (sort by `(kind, name)`, `cli.py:138`). A mutated summoned unit gets a new content sha; the lockfile pins by that sha so un-versioned mutation can't enter a frozen closure.

**Acceptance criteria.** `resolve` returns a `Lockfile` with every transitive `DefinitionRef` pinned to exact version + `sha256:` integrity. Incompatible ranges ⇒ `ResolutionError` naming both requirers. `^1.2` ⇒ highest `1.x ≥ 1.2`; `~1.2` ⇒ highest `1.2.x`. Identical inputs ⇒ identical `closure_sha()` across machines. `craw freeze` writes a transitive closure; `--check` exits non-zero on drift. A run records `closure_sha()` (reference). No new third-party dep.

**Dependencies.** Soft: summonable units (AL-DV4) for the mutable-unit pinning story; ships standalone over `DefinitionRef`/`Registry`. **Effort.** L. **Open (resolved):** a mutable summoned unit (Wiki at HEAD) is snapshotted at `freeze` time; later mutation forces a re-freeze (`--check` catches drift). v1 supports exact pins + `^`/`~` only, erroring on unsupported grammar.

---

## Milestone 6 — Definitions-as-versioned-variables + summonable knowledge

> Git-for-Definitions and the knowledge-object substrate. The single load-bearing refactor: extract `_content_sha`/`_refreeze`/`_with_agents` from `tuner.py` into `definition/derive.py` so the Tuner and the public API share **one** content-hash path.

### ISSUE AL-DV1 — `with_skill`/`with_context`/`with_agent`: copy-on-write Definition composition (NEW)

**Problem.** `Definition` is `Freezable` but has no public composition API; the only derivation is the Tuner's private `_with_agents` (`tuner.py:119-123`).

**API design.** Copy-on-write methods, each returning a **new frozen** Definition (deep-unfrozen `model_copy` → structural edit → `_refreeze` → new `Version.sha`); the receiver is never mutated.

```python
def with_agent(self, agent, *, replace=False) -> "Definition": ...
def with_skill(self, skill) -> "Definition": ...              # SkillRef pins a version
def with_context(self, obj: "Summonable", *, mode="readonly") -> "Definition": ...   # SummonRef {id,version,mode}
def with_inputs(self, *params) -> "Definition": ...
def with_policy(self, policy) -> "Definition": ...
```

A new `summons: list[SummonRef]` field (alongside `dependencies`, `definition/types.py:122`); the summoned unit's pinned hash folds into `export().checksum` without embedding mutable content (reference-not-embed).

**Determinism constraints.** Every op routes through `_refreeze` → new sha; copy-then-seal makes un-versioned mutation impossible; summons enter identity by pinned hash; pure deterministic structural transforms.

**Acceptance criteria.** `base.with_agent(a)` ⇒ `frozen is True`, `version.sha != base.version.sha`, `base` unchanged. Two structurally-identical compositions ⇒ equal sha (idempotent); any knob diff ⇒ distinct sha. `with_*` on the receiver never raises `FrozenError` (copies first); mutating the *returned* frozen object directly raises. `with_context(wiki)` stores only a `SummonRef`; `export().checksum` changes iff the pinned summon version changes. A `from_package` round-trip recompiles byte-identically.

**Dependencies.** F-7 for `.readonly()/.mutable()` narrowing (until then `mode` defaults `"readonly"`). **Effort.** M. **Open (resolved):** `with_context` of a mutable summonable pins a **snapshot hash at compose time** (a moving pointer is `recall`).

### ISSUE AL-DV2 — `save`/`recall`: a name→hash registry (git for Definitions) (NEW)

**Problem.** Composition gives hashes but no names. Git's ergonomic is mutable name pointers into an append-only immutable object store. Crawfish has the immutable side and a discovery `Registry`, but no version log / `save`/`recall`.

**API design.** A Store-backed, append-only, org-scoped `DefinitionStore`: `save(name, d, *, parent)` (requires `d.frozen`; the pointer move is the only mutation; idempotent on sha), `recall(ref)` (resolves `name` / `name@sha` / bare sha; pure), `log(name)` (the lineage). Shares one `VersionRecord` type with `LearningLoop`. Registry stays discovery-only (F-6/OPT-4 reconciliation).

**Determinism constraints.** Pointer move is the sole mutation; object store append-only + content-addressed; `recall` is pure; tenancy on every row; no model call.

**Acceptance criteria.** `save("extract", d)` then `recall("extract")` ⇒ same sha; saving an unfrozen Definition raises. `recall("extract@<sha>")` resolves a historical pointer after `name` moves on. Byte-identical Definitions store content once (dedup) but record two pointer events. `log` returns a correct `parent_sha` chain; cross-org isolation. `recall` never mints a new sha.

**Dependencies.** AL-DV1, Store. **Effort.** M. **Open (resolved):** `recall` resolves exact sha or current pointer, not a SemVer range (the solver is OPT-4); GC keeps orphans for `craw share` reproducibility.

### ISSUE AL-DV3 — `modify`/`reset`: git-like checkout over the version log (NEW)

**Problem.** With names + CoW edits, the remaining git verbs are branch-local mutation (`modify`) and pointer rewind (`reset`).

**API design.** `modify(store, name, fn)` = `recall → fn (a with_*-composed Definition) → save(parent=old)` (the only verb that advances a pointer to new content; requires the result be frozen). `reset(store, name, to)` = a pure pointer move (mints no content; refuses a `to ∉ log(name)`). `modify` is legal only in **train mode**; an eval-mode name is read-only and `modify` raises.

**Determinism constraints.** `reset` mints no content; `modify` routes through `_refreeze`; no in-place edit; no model call.

**Acceptance criteria.** `modify` advances the pointer, records `parent_sha`, leaves the old sha recallable via `@sha`. `reset` returns the pointer, mints no object, rejects an unreachable sha. After `reset`, `recall` and the original `@old_sha` return content-equal Definitions. `modify` on an eval-mode name raises. Same start + pure `fn` ⇒ same resulting sha.

**Dependencies.** AL-DV1, AL-DV2; train/eval mode (AL-T1). **Effort.** S. **Open (resolved):** three-way `merge` is deferred to R1's typed-diff work; `reset` never prunes orphans by default (preserves `craw share` reproducibility).

### ISSUE AL-DV4 — Summonable knowledge objects: `Wiki`/`Rag` (NEW, `Rag` deferrable)

**Problem.** The vision wants knowledge objects you *summon* (`feature_loop(summon=[arch.readonly(), playbook.frozen()])`). The partial substrate is `Memory` (`memory.py:32-81`) and the `Context` artifact (`runtime/context_artifact.py:71-110`) — neither is a versioned, summonable, narrowable unit.

**API design.** Both `Freezable` + `Summonable`, storage via Store/ArtifactStore seams. `Wiki` (typed pages reusing `ContextEntry`, `context_artifact.py:47`; `with_page` is CoW → new sha; a tainted page stays tainted). `Rag` (retrieval over a content-hashed corpus snapshot; the **index version** = corpus sha + embed-model id + chunker config is the content hash, so retrieval over a frozen `Rag` is **replay-deterministic** — same `(query, version)` ⇒ same hits; only re-indexing, a train-mode op, mints a new sha). Both expose `.readonly()`/`.mutable()`. Summoned via `with_context` as a `SummonRef`.

**Determinism constraints.** Both `Freezable`; all edits CoW to a new sha. Summons enter identity by pinned hash (vision §5). Retrieved content is **FLUID/tainted by default** — reaches the model as data, never instructions, never a static-only Sink target; taint propagates into the `Context` (`context_artifact.py:91-110`). `Rag.retrieve` over a frozen index is pure (query, version) — not a stochastic primitive.

**Acceptance criteria.** `wiki.with_page(...)` ⇒ new frozen `Wiki` with a distinct sha; receiver unchanged; mutating a frozen `Wiki` raises. Frozen `Rag` at V: `retrieve(q,k=3)` returns identical `ContextEntry` ids on two calls and under replay; re-indexing ⇒ new sha. Summoned content lands `tainted=True` by default, never reaching an instruction slot or static-only Sink. `wiki.readonly()` cannot write; `wiki.mutable()` is rejected in eval mode. A summoned unit's pinned sha appears in `export().checksum`; its body does not.

**Fix applied (SECURITY missing):** `Rag` embeddings route through the **secret-scrubbing seam** (`ScrubbingStore`, `secrets.py`) so no secret lands unredacted in an index (an acceptance test).

**Dependencies.** AL-DV1 (`with_context`/`SummonRef`), F-7 (borrow), Store/ArtifactStore. **Effort.** L (`Wiki` M, `Rag` the larger half — `Rag` is **deferrable** to a follow-on). **Open (resolved):** a large corpus snapshot's hash is a **Merkle over chunks** so re-index only re-hashes changed chunks (Bazel-style).

### ISSUE AL-DV5 — `state_dict()`/`load_state()` (NEW)

**Reviewer dedup:** identical to **AL-T2** (the ML-library spec). **Canonical issue is AL-T2.** AL-DV5 contributes the summons-by-reference requirement (already folded into AL-T2's `summoned_refs`) and the "lives in this area because a state_dict is the variable's value decoupled from its type" framing. No separate ticket.

---

## 6. Dependency / sequencing graph (text)

```
FOUNDATIONS (Milestone F) — build first, single-owner each:
  F-0 output_content_sha ─┬─> CL-1, CL-4, C2, C3, R1, R3, TS-1
  F-1 cassette-key/coordinate (owns runtime/replay.py) ─┬─> TS-1, TS-8, R3, CL-4, C2
  F-2 loop/program ledger (owns ledger ext) ──> CL-4, C2b, C3
  F-3 gate algebra (owns eval gate) ──> CL-2, AL-T5, TS-7/R4, AL-T6, AL-T3/TS-6
  F-4 from_corrections + correction event ──> CL-2, TS-7/R4
  F-5 decode-knob ADR ──> AL-T1, AL-T2, TS-8
  F-6 cost-model single owner (owns cost.py) ──> OPT-2 (= the issue), CL-3/ALG-5 consume
  F-7 borrow semantics ──> ALG-4, AL-DV1(soft), AL-DV4
  F-8 experiment-design spec ──> AL-T4/TS-2, AL-T5, AL-T6, TS-1, TS-7/R4, AL-T3/TS-6  (cross-cutting acceptance gate)

CONTROL PLANE:
  F-0,F-1,F-8 ──> CL-1 ──> CL-4 (needs F-2)
                  CL-1 <── CL-2 (VerifierStop; needs F-3,F-4)
COMPOSITION:
  C1 (standalone, lands on main) ──> C2a (needs F-0,F-1) ──> C2b (needs F-2) ──> C3 (needs F-8 no-progress)
  C2 is the substrate the durable Refine (CL-4) and recurse (C3) generalize onto.
ML LIBRARY:
  F-5 ──> AL-T1 ──> AL-T2(=AL-DV5)  (knob membership)
  F-8 ──> AL-T4/TS-2 ──> AL-T5 ──> (baseline+std)
                         AL-T4 ──> TS-4, AL-T3/TS-6 (ece term), AL-T6, CL-1/C2/C3 (no-progress band)
TAMING:
  F-1 ──> TS-1 (needs AL-T4 band, CL-2 tie-break, TS-4 fallback)
  TS-4 (needs AL-T4)
  F-3,F-4,F-8 ──> TS-7/R4
  F-1,F-5 ──> TS-8 (standalone, ship early)
SURFACES:
  OPT-1 (needs CL-1 for `refine`, TS-7/R4 for `guard`; eval/tune/learn standalone)
  OPT-2 (= F-6; consumed by CL-1 preflight, ALG-5, CL-3)
  OPT-3 (standalone)
  OPT-4 (standalone; soft AL-DV4)
VARIABLES/KNOWLEDGE:
  derive.py refactor ──> AL-DV1 ──> AL-DV2 ──> AL-DV3
  AL-DV1 ──> AL-DV4 (Wiki now, Rag deferrable), AL-T2
PROPERTY ALGEBRA (Milestone behind a spike — see §7 note):
  ALG-1 ──> ALG-2 ──> ALG-3 (pull-forward only the 2-pt fluid→static-sink check) ──> ALG-4(F-7) ──> ALG-5(consumes OPT-2) ──> ALG-6 ──> ALG-7
REVOLUTIONARY:
  R1 (needs C2 content-sha + F-0) ──> R3 (needs C2b checkpoint), R4(=TS-7/R4), R5(=AL-T2 + AL-T1)
  R2 (SPIKE-GATED: decidability of the 2-pt lattice; sound-but-incomplete only)
```

**Longest dependency chain (critical path):** `F-1/F-2 → C1 → C2a → C2b → C3` and `F-3/F-8 → AL-T4 → AL-T5`. Fund the C2 checkpoint substrate once; CL-4, R3, and durable resume all fall out.

### Milestone-0 flagship slice (shipping BLOCKER fix — names a C2-free value-fastest slice)

A demoable, low-risk slice that proves the train/eval thesis **without** the XL keystones (C2/ALG-4/R2):

1. **CL-1 `Refine`** (standalone runtime wrapper, `_checkpoint` stubbed) — the headline operator.
2. **AL-T4/TS-2 `cw.calibrate`** (independent measurement — "measure before you tame").
3. **OPT-3 single-flight cache** (independent cost win).
4. **OPT-1 `craw eval/tune/learn`** (drives existing libraries; `refine`/`guard` deferred).
5. **C1 runnable Router** (S, lands on `main`).

Prerequisites for the slice: F-0, F-1, F-8 (and F-3 for AL-T4's gate hook). Defer C2/C2b/ALG-*/R2 to later milestones explicitly.

---

## 7. Determinism invariants every issue must uphold (from the anchor's `must_not_break`)

1. **Determinism thesis.** The agent call stays the **only** stochastic primitive, behind the single `AgentRuntime` ABC (`runtime/base.py:11`). Control flow, gating, scoring, promotion are pure deterministic Python.
2. **Un-versioned mutation is forbidden.** Every mutation flows through content-hashed copy-on-write minting a new `Version.sha` — never in-place on a shared/frozen object (`Freezable.__setattr__`, `version.py:59-62`). Train = unfrozen; eval = frozen + replayable. A run records which version it saw.
3. **Three seams stay protocol-only.** Nodes import `AgentRuntime`/`Store`/`ArtifactStore` protocols, never a concrete backend; no SDK import in nodes; no raw SQL outside a `Store`. New operators drive runs through `AgentRuntime`.
4. **Flow / taint boundary.** `Flow.FLUID` is untrusted session data — reaches the model as data, never instructions. Sink targets + idempotency keys are static-only. Taint propagates through every carry/summarize/iterate/aggregate operator (**aggregate taint = union of input taints**); the Tuner/LearningLoop mutate only STATIC config (`learning.py:33-34`).
5. **CostBudget gas.** Every model call charges one **shared** budget (hard-kill, `context.py:42-47`); iterate/recurse/quorum operators charge **every** iteration and preflight `remaining_usd` (`run.py:320-325`). Bounding is `max_iters`/`max_visits`/`max_depth` + budget + cancel + **calibrated** no-progress — **never wall-clock**.
6. **Cancel + checkpoint.** Every composition step `raise_if_cancelled` cooperatively and checkpoints per step (`workflow.py:136-139`); back-edges/loops checkpoint each iteration **atomically over (body output + verifier verdict)**.
7. **Assembly-time type check** stays structural via `crawfish.typesystem`, never string equality (ADR-0002); new nodes/edges pass `check_types`.
8. **Eval gate is the safety net.** Self-modification graduates **only** if it beats baseline on the `GoldenSet` (paired, family-wise-corrected, winner's-curse-corrected per F-3/F-8). Exploration may fail safely and cost budget but **never degrades the promoted best**. The absolute-precision gate (verifiers/guards) **fails closed**.
9. **Tenancy.** Every persisted artifact (state dicts, calibration reports, declass records, learned rules, loop ledger rows, name pointers, Rag indices) carries `org_id` and routes through the **secret-scrubbing seam** (`ScrubbingStore`) — including direct `put_record` writes.
10. **Replay reproducibility.** Cassette key = `sha256(version+inputs)` plus the F-1 execution coordinate; each tuned/iterated candidate is a distinct re-frozen artifact with a distinct sha so candidates never collide on replay. `state_dict`/`load_state` are content-hashed and reference summoned units by pinned version, not embed.
11. **Defense in depth (R2).** The assembly-time taint check **never replaces** the runtime `StaticOnlyError`/`TargetMustBeStaticError`; any construct outside the proven fragment **fails closed** (rejected, not open). `declassify` must be **unreachable from a fluid-tainted code path** (no confused-deputy upgrade).

---

## 8. Open questions (consolidated)

1. **Decode-knob ownership** (F-5): tunable `temperature`/`top_p`/`sample_k` in the content hash vs `grammar`/`decode_seed` on `RunRequest`; which enters `_key` vs `version.sha`; the `AgentRuntime` determinism capability tier and the infra variance floor. *Resolved by F-5 ADR; listed because it gates AL-T1/AL-T2/TS-8.*
2. **R2 static decidability**: is the 2-point taint lattice statically decidable over the dataflow graph with summarization/carry/agent-leaf declassification? *Spike-gated; only a sound-but-incomplete result over the linear+branch+bounded-refine fragment is in scope.*
3. **Summoned-unit identity** (vision §5): snapshot ref vs full embed. *Resolved toward reference-by-version everywhere (R1, R5, AL-T2, OPT-4); a mutable unit is snapshotted at `freeze` time and re-freeze is forced on drift.*
4. **`recurse` vs `feature_loop`**: distinct or a self-summoning back-edge? *Resolved: a depth-guarded `Program` back-edge sharing C2's kernel (C3).*
5. **Borrow lifetime** (F-7): when a `mutable()` borrow begins/ends across async; Store-backed atomic claim vs lexical `train()` scope. *Resolved toward a context-manager protocol + Store-backed atomic claim.*
6. **Gate algebra reconciliation** (F-3): relative-regression vs variance-aware-LCB vs absolute-precision — which consumer uses which, paired tests, family-wise correction, Clopper-Pearson only for binary metrics.
7. **`expected_usd` conditioning** (OPT-2): what the expected band is conditioned on and how rates carry uncertainty (CI-aware, not a point estimate).
8. **Online graduation rigor** (AL-T6): anytime-valid sequential test vs pre-registered N; where UCB/Thompson live on `SearchStrategy`; how explore budget is metered distinctly (`explore=True` tag).
9. **Calibration methodology** (AL-T4): Brier/NLL primary vs ECE diagnostic; adaptive/equal-mass binning; bootstrap CI; small-`GoldenSet` power.
10. **Learned-predicate grammar** (TS-7/R4): the fixed, closed, side-effect-free expression grammar; where learned ASTs live as content-hashed artifacts; the `correction` ledger event-kind source set.
11. **Three-way `merge` semantics** (R1/AL-DV3): conflict granularity on prompt *text* vs structured knobs. *Deferred.*
12. **Property-algebra timing**: should the `Grade` unification ship at all near-term, or stay behind a spike with only the ALG-3 2-point fluid→static-sink check pulled forward? *See §7 note below.*

> **§7 note — Property-Algebra scope (shipping major fix).** Reviewers (shipping-pragmatist) judged ALG-1..7 the most over-scoped, most premature area: it rebuilds four working, enforced mechanisms atop a brand-new `Grade` semiring with high regression risk and no user-facing capability until many issues land. **Accepted with modification:** the `Grade` unification (ALG-1/2/4/5/6) is **deferred to a post-flagship research milestone gated by a spike**, and **only ALG-3's narrow goal — assembly-time fluid→static-sink rejection as a 2-point extension to the existing `parameters_compatible`/`check_types`, not predicated on `Grade`** — is pulled forward (it is the security strengthening every reviewer praised, and it underpins R2). The full algebra issues are retained in the appendix for completeness but are **not** Milestone-0/1 work. *Rejected the reviewer's implication to drop ALG-7 entirely:* the non-interference conformance suite (with the explicit aggregate-taint-union and declassify-not-from-fluid invariants) is kept as the cross-cutting acceptance gate, because it is the proof artifact that the determinism thesis is upheld — but scoped to the executable suite over the finite operator set, with the formal proof flagged as a research-frontier follow-on (PL/SECURITY agreed).

---

## 9. Appendix — Linear epic structure (ready to file)

**Epic:** `The Agent Language` — deterministic agent language + tunable ML library; `mutable` = train/eval mode.
**Suggested epic labels:** `epic:agent-language`, `thesis:determinism`, `area:language`, `area:ml-library`.

### Milestone F — Foundations *(labels: `foundation`, `blocking`, `one-owner`)*
- **F-0** `output_content_sha` helper; reuse `Output.derive()` (correct grounding: Output already frozen). *`security`, `versioning`.*
- **F-1** Canonical cassette-key + execution-coordinate schema (owns `runtime/replay.py`). *`security`, `replay`, `blocking`.*
- **F-2** Loop/program composite-key ledger + deterministic `loop_id` (NEW schema, not reuse). *`ledger`, `durability`.*
- **F-3** Gate algebra: reconcile relative/variance/precision gates; paired+family-wise; precision fails closed. *`eval`, `stats`, `one-owner`.*
- **F-4** `GoldenSet.from_corrections` + `correction` ledger event kind. *`eval`, `corpus`.*
- **F-5** Decode-knob ownership ADR + capability tier (`temperature` in hash, `grammar` per-call). *`adr`, `runtime`, `blocking`.*
- **F-6** Cost-model single owner (owns `cost.py`); multiplicative composition law. *`cost`, `one-owner`.*
- **F-7** Borrow-lifetime / mode operational semantics (context-manager + Store-backed atomic claim). *`semantics`, `concurrency`.*
- **F-8** Experiment-design spec (estimands, paired tests, held-out split, power, anytime-valid, winner's-curse). *`stats`, `cross-cutting`.*

### Milestone 1 — Control plane *(labels: `control-plane`, `flagship`)*
- **CL-1** `Refine`: verifier-gated iterate-until-goal operator (canonical; absorbs TS-5). *`L`.*
- **CL-2** `Verifier`: gated external-signal critic (absolute-precision, fail-closed). *`L`, `security`.*
- **CL-4** Durable, crash-resumable `Refine` over the F-2 ledger + cassettes. *`M`, `durability`.*

### Milestone 2 — Composition surface *(labels: `composition`, `structural`)*
- **C1** Runnable `Router`/`branch`. *`S`, `quick-win`.*
- **C2a** `Program` driver + cyclic `check_types` + `UnboundedCycleError`. *`L`.*
- **C2b** Per-iteration ledger versioning + durable resume. *`M`.*
- **C3** `recurse`: bounded self-referential back-edge with `combine`. *`L`.*

### Milestone 3 — Tunable ML library *(labels: `ml-library`, `unifier`)*
- **AL-T1** Two-axis mode: per-knob `tunable` + `train()`/`eval()`. *`M`.*
- **AL-T2** `state_dict()`/`load_state()` (canonical; = AL-DV5). *`M`.*
- **AL-T4/TS-2** `cw.calibrate()` (Brier primary, ECE diagnostic+CI, capability tier). *`L`.*
- **AL-T5** Variance-aware promotion gate (paired, corrected). *`M`.*
- **AL-T3/TS-6** `Objective`: cost-regularized loss (normalized λ + ε-constraint + Pareto). *`S/M`.*
- **AL-T6** Explore dial: decaying-ε + anytime-valid graduation + UCB/Thompson hook. *`L`.*

### Milestone 4 — Taming stochasticity *(labels: `taming`, `reliability`)*
- **TS-1** Typed quorum/self-consistency (sequential early-stop, abstain on ill-defined plurality). *`M`.*
- **TS-4** Abstention as a typed Output (calibration-derived threshold). *`S`.*
- **TS-7/R4** House-guard: learned-then-distilled guards with precision-AND-coverage certificate (closed grammar). *`L`, `security`.*
- **TS-8** Constrained decoding as a runtime-call property (decode fields enter run identity). *`M`, ship early.*

### Milestone 5 — Surfaces & accuracy *(labels: `dx`, `cost`, `cli`)*
- **OPT-1** `craw eval/tune/refine/learn/guard` (mandatory versioned `--json`). *`L`.*
- **OPT-2** Honest cost interval (= F-6; escalate/repair/retry/refine multipliers). *`M`.*
- **OPT-3** Live single-flight caching. *`M`, cost win.*
- **OPT-4** Dependency resolver + lockfile (pure SemVer `^`/`~`, closure_sha). *`L`.*

### Milestone 6 — Variables & knowledge *(labels: `git-for-agents`, `knowledge`)*
- **derive.py refactor** Extract `_content_sha`/`_refreeze`/`_with_agents` (load-bearing). *`S`.*
- **AL-DV1** `with_skill`/`with_context`/`with_agent` (CoW + `summons`). *`M`.*
- **AL-DV2** `save`/`recall` name→hash registry. *`M`.*
- **AL-DV3** `modify`/`reset` (train-mode-gated). *`S`.*
- **AL-DV4** `Wiki` (now) / `Rag` (deferrable); scrub-on-index. *`L`.*

### Milestone 7 — Revolutionary capabilities *(labels: `revolutionary`, `frontier`)*
- **R1** Git-for-agents: `Program.content_sha`, `craw diff`, `craw merge`. *`L`.*
- **R2** `craw prove --no-injection`: assembly-time non-interference (**sound-but-incomplete; spike-gated**). *`XL`, `research`, `spike-first`.*
- **R3** `craw replay --swap`: counterfactual time-travel (cost-bounded cascade). *`M`, `dx`.*
- **R4** = TS-7/R4 (guards as content-hashed program members). *cross-listed.*
- **R5** = AL-T2 + AL-T1 (Hugging-Face-for-agent-weights). *cross-listed.*

### Milestone 8 — Property/Capability Algebra *(labels: `pl-theory`, `spike-gated`, `post-flagship`)*
> Deferred per the §7 note; only ALG-3's 2-point fluid→static-sink check is pulled forward (into Milestone-1-adjacent security work). The rest is gated by a spike.
- **ALG-3 (pulled forward)** Assembly-time fluid→static-sink rejection on existing `parameters_compatible`/`check_types` (2-point, default-equivalent to today). *`L`, `security`.*
- **ALG-1** `Grade` product-graded type + `Graded` protocol (deferred). *`L`.*
- **ALG-2** `narrow()`/attenuation, monotone-down (deferred). *`M`.*
- **ALG-4** Mutability borrow = train/eval via CoW; dynamic exclusive borrow (F-7). *`XL` → split.*
- **ALG-5** Cost coeffect grade (consumes OPT-2). *`M`.*
- **ALG-6** `declassify`: the sole audited upgrade; unreachable from fluid paths. *`M`.*
- **ALG-7** Non-interference conformance suite (aggregate-taint-union + no-declassify-from-fluid invariants; finite operator set). *`L`, cross-cutting acceptance gate.*
