# Crawfish as a Deterministic Agent Language — Audit

**Thesis under test:** *Crawfish is a deterministic programming language for agents — the
agent call is the only stochastic primitive; types, control flow, effects, cost, memory, and
concurrency around it are ordinary deterministic code.* The four goals it serves: maximize
**determinism**, lower **cost**, improve **speed**, improve **quality**, by pushing
nondeterminism to the leaves and keeping the program deterministic.

**Method:** read-only audit by a 5-agent team — a Language Auditor (construct inventory), a
Determinism/Cost/Speed/Quality Auditor, a Composition/DX Auditor (who *wrote and ran* a
refine-until-good program against the public API), a Researcher (web comparisons), and a
Skeptic with veto power who killed 8 of 15 candidate findings. Every surviving claim is
grounded in `file:line` or an executed sandbox run. Date: 2026-06-22. Audited against `main`
(v0.2.0, `crawfish.__all__` ≈ 333 symbols), plus the untracked `runtime/escalate.py` and
`batch.py` parallelism change.

---

## 1. Verdict

Crawfish is **a coherent, genuinely deterministic *data-plane* language for agents with an
incomplete *control plane*.** The thesis holds where it counts most subtly: the agent call is
the only stochastic primitive, isolated behind one `AgentRuntime` ABC, and everything wrapped
around it — structural types checked at assembly *and* validated at runtime, a real
static/fluid (`Flow`) information-flow/taint effect system with static-only Sink targets, cost
as enforced gas (`CostBudget` hard-kills; `estimate_cost` is static analysis through the *same*
resolver the runtime uses, so preview can't drift), content-hash freezing + replay cassettes,
and a clean PGO analogue in `Tuner`/`LearningLoop` — is ordinary deterministic Python. These
cohere because they all funnel through three seams (Runtime/Store/ArtifactStore) and one
`RunContext` carrying budget + cancel token, so determinism, cost, and observability are
*uniform properties*, not per-feature add-ons. On the **language claim specifically** it is
ahead of DSPy and LangGraph on determinism-by-construction (it refuses both the learned
compiler and the cyclic graph) and sharper than classical effect systems in collapsing all
stochasticity into one typed primitive.

But the program-assembly surface is a flat, forward-only, **cycle-rejecting** step list
(`Source→Batch→Aggregator/Filter→Sink`), and that is the ceiling. **The three biggest things in
the way:**

1. **No iterate-until-goal operator.** There is no bounded `while`/`until` that re-runs the
   stochastic leaf until a judge passes, a `Rubric` clears a threshold, or a typed predicate
   holds — guarded by `max_iters` + `CostBudget` + no-progress detection. This is the single
   most characteristic agentic control structure, and it's the one the language doesn't provide.
2. **No executable composition surface beyond a single linear pipeline.** `Workflow.run` and
   `Engine.run_pipeline` are `for step in steps` forward passes; `BatchExecutor` *rejects*
   cycles (`CycleError`); and `Router` — the branch primitive — **is not even a runnable
   Workflow step** (`_run_step` raises `TypeError` on its kind). Branch-then-recurse is
   unbuildable as a Crawfish artifact today.
3. **When control flow is hand-rolled in Python, the guarantees evaporate.** The DX auditor's
   refine loop ran with `spent=0.0` because nothing forces a user loop to charge, share one
   budget, type-check, checkpoint, or carry taint/lineage. The framework's safety is available
   *only* for the linear pipeline shape.

The honest one-liner: **Crawfish is a deterministic data-plane language wearing a fixed-topology
pipeline for control flow.** Closing gaps (1) and (2) is exactly what turns it into the
*program/app* surface this project is aiming for — and into a target language strong enough for
**craw code** (the planned Claude Code fork/plugin) to *emit* rigid agent work into, rather than
generate brittle glue.

---

## 2. Construct inventory

Ratings: **first-class** / **partial** / **missing**. Goals each construct actually serves:
**D**eterminism · **C**ost · **S**peed · **Q**uality (· *none* = exists but exploits no goal).

| Construct | Rating | Key evidence | What "done" needs | Goals |
|---|---|---|---|---|
| Functions (typed, composable Definitions) | **first-class** | `definition/types.py:109-145`, `run.py:75-110` | Versioned, freezable, typed-IO unit; `Run` binds inputs → typed `Output` | D Q |
| Static types + assembly-check + runtime output validation | **first-class** | `batch.py:95-111`, `run.py:113-132,298-337`, `typesystem/registry.py` | Structural compat (not string eq); reject mistyped wires pre-call; validate in/out | D Q |
| Effect system: `Flow` static/fluid + taint + Sinks as I/O boundary | **first-class** | `core/types.py:36-46`, `run.py:253-262`, `nodes/sink.py:46-114` | FLUID = injection boundary; taint propagates; Sink targets static-only | D Q |
| Sequence + Conditional/switch (Router + Classifier) | **first-class** *(but see Gap #2 — not Workflow-runnable)* | `nodes/router.py:61-214` | Closed label set + mandatory default; unroutable rejected at assembly | D Q |
| Loop (a): map / fan-out (Batch) | **first-class** | `batch.py:45-194` | Multi-Source → one Run/item; shared budget; bounded-semaphore concurrency | S C |
| **Loop (b): iterate-until-goal** | **missing** | `__init__.py:361-718` (no operator); `escalate.py:78-109`; `run.py:309-337` | A guarded `Refine/Until/While`: re-run until judge/Rubric/predicate, capped by max_iters + budget + no-progress | *none* |
| Recursion (Definition → sub-Definitions to a base case) | **partial** | `definition/types.py:40-62,122`, `runtime/team.py` | `delegates_to`/`dependencies` give delegation; no self-referential invocation with a termination guard | Q |
| Error handling (retry / dead-letter / validation repair) | **first-class** | `retry.py:33-113`, `run.py:278-337`, `validation.py` | RetryPolicy (never retries budget/cancel); REPAIR = one bounded re-prompt; DEAD_LETTER | Q C |
| Variables / memory / scoping | **first-class** | `memory.py:32-81`, `runtime/context_strategy.py`, `core/context.py:74-97` | Store-backed KV scoped by (namespace, org_id); typed Context + carry strategies | D C |
| Evaluator/VM (AgentRuntime; MockRuntime reference interpreter) | **first-class** | `runtime/base.py:82-125`, `runtime/mock.py:27-44` | One SDK seam; MockRuntime a pure function of the request; many backends | D C S |
| Reproducible execution (content-hash, freeze, replay cassettes) | **first-class** | `versioning/version.py`, `runtime/replay.py:25-39`, `testing.py` | Freeze rejects mutation; cassette key = sha256(version+inputs); offline tests | D Q |
| Memoization / caching | **first-class** *(scoped — see Gap #4)* | `cache.py:32-122`, `runtime/replay.py:25-39` | Cassette hit = $0, no budget charge; key is the content hash | C S D |
| Cost as gas (`CostBudget`; `estimate_cost` static analysis) | **first-class** *(estimate blind to escalate/repair — Gap #5)* | `core/context.py:32-53`, `cost.py:92-142` | `charge` hard-kills; estimate shares runtime's resolver; budget threads everywhere | C D |
| Compiler optimization / PGO (Tuner; LearningLoop; evals) | **first-class** | `tuner.py:392-587`, `learning.py:92-263`, `eval.py` | Seeded, bounded, regression-gated search; eval-gated reversible promotion | Q C |
| Structured concurrency (fan-out, BatchExecutor, fan_in) | **first-class** *(no typed quorum — taste)* | `executor.py:51-234`, `batch.py:168-194` | Kahn topo (rejects cycles); bounded pool; budget breach cancels in-flight | S C |
| Modules / versioned imports / package manager | **partial** | `discovery.py:42-117`, `definition/types.py:69-71,100-106` | Discovery + DefinitionRef + capabilities exist; no SemVer solver/lockfile; hub is a stub | D Q |
| Assertions / property tests (Metric/Rubric/Benchmark) | **first-class** | `metrics.py:31-55`, `eval.py`, `testing.py:302-317` | Score → float; Benchmark over fixed set; `compare`/`gate_against_baseline` | Q D |
| Debugger / profiler / tracing (emission, inspector, dashboards) | **first-class** | `emission.py`, `inspector.py`, `anomaly.py` | Typed OTel-shaped emission ledger with taint; inspect/tail/report; auto-halt | Q D |
| Capabilities / sandbox (jail, secret broker, consent) | **first-class** | `jail.py`, `secrets.py:197-240`, `sandbox.py` | Jail w/ taint + StaticOnlyError; secrets by reference; consent deciders | D Q |

**Coherence note:** the data plane and effect/optimization planes are uniformly first-class and
cohere through the three seams. The one load-bearing hole is the **control plane around the
leaf**: map (first-class) and switch (first-class but not Workflow-runnable) are present;
**guarded iterate-until-goal and bounded recursion are absent**, and the absence shares a single
structural root (§3, Gap #2).

---

## 3. The headline gaps (ranked, skeptic-survived)

The skeptic killed 8 candidate findings (escalate-as-standalone-defect, the package-manager
deferral, quorum/voting taste, session_id footgun, static-routing-is-by-design, the
nondeterminism-is-confined corroboration, and cost-not-statically-typed taste). Seven survived.
They collapse into two flagship gaps plus three cost/accuracy leaks.

### Gap #1 — No first-class iterate-until-goal operator *(HIGH)*
`crawfish.__all__` has no `Loop`/`Until`/`While`/`Refine`/`Reflect` — the only loop-named symbol,
`LearningLoop`, is an *offline* version-promotion loop. The three adjacent atoms are all
**fixed-bound, not goal-driven**: `EscalatingRuntime` re-runs exactly once (primary→strong, 2
attempts max — `escalate.py:99-109`); `Run._repair` re-prompts exactly once on a schema failure
(`run.py:309-337`); `RetryPolicy` re-runs on *exceptions*, never reading a quality signal
(`retry.py:62-80`). The framework's whole pitch is "wrap deterministic, guarded code around the
stochastic leaf" — yet the most common agentic need that demands guarded determinism (loop the
model until output is good, bound the spend, detect non-progress) is the construct that's
missing. *Skeptic: survives — the deferral doc acknowledges the gap; it does not make the thesis
hold today.*

### Gap #2 — No executable composition surface beyond a single linear pipeline *(HIGH; structural root)*
`Workflow.run` is `for i, step in enumerate(self.steps)` (`workflow.py:133-142`);
`Engine.run_pipeline` is identical (`engine.py:43-49`); `BatchExecutor` Kahn-layers and **raises
`CycleError`** on any back-edge (`executor.py:85-87`). `_run_step` handles only
Source/Filter/Batch/Aggregator/Sink and **raises `TypeError` on any other kind** — so **`Router`
is not a runnable Workflow step** (`workflow.py:153-187`); `Router.route` merely returns a
`(label, Node)` tuple the caller must dispatch by hand (`router.py:201-214`). There is no
back-edge any construct could use: a guarded loop is **unrepresentable in the assembly surface**,
not merely unshipped. This is the structural cause of both Gap #1 and the recursion gap. *Skeptic:
survives — and it is also the enforcement that makes determinism-by-construction real; the same
restriction is both the strength and the limit.*

### Gap #3 — Hand-rolled control flow loses every framework guarantee *(MEDIUM)*
The DX auditor's `refine.py` ("refine until `Rubric ≥ 0.8`, max 5, bounded by `CostBudget`") ran
correctly — but the framework contributed only **one** `Run.execute()` (the leaf) and **one**
`Rubric.score()` per turn. The iteration bound, until-condition, early-return, state carry, *and
the budget-exhaustion guard* were all hand-rolled plain Python. The budget read `spent=0.0` after
real iterations because nothing forces a user loop to charge or share one `CostBudget`. The
built-in cascades *do* enforce it (`run.py:323-325` pre-flights `remaining_usd` for REPAIR),
proving the pattern exists but is **not exposed as a reusable guarded-loop bound**. *Skeptic:
survives — sharpest demonstration of the thesis hole; cost-coupling is by user discipline, not by
construct.*

### Gap #4 — Caching only fires on pre-recorded cassettes *(MEDIUM, cost)*
`CachingRuntime` wraps `RecordReplayRuntime` *only* (`cache.py:68-69`). On a first **live** pass
over a batch with duplicate `(definition+inputs)` items, each item makes its own model call —
there is no single-flight / LRU on the live provider path (`batch.py:168-185`). The thesis sells
caching as a cost lever that collapses identical calls to $0; on first live execution that lever
doesn't fire. *Skeptic: survives — genuine gap in the cost story, not a documented deferral.*

### Gap #5 — Static `estimate_cost` is blind to escalation / repair / retry *(MEDIUM, cost)*
`estimate_cost` charges "one run per agent per item" (`cost.py:100-102,124-134`) and has zero
awareness of `EscalatingRuntime`'s 2× tail, the one REPAIR re-prompt, or up-to-3 RetryPolicy
attempts. It's drift-free with the routed run on the happy path, but is a strict **lower bound**
that systematically understates the cost-amplifying paths. The hard `CostBudget` kill is the real
ceiling, so it's bounded-not-unsafe, and the docstring concedes "planning aid, not a guarantee."
*Skeptic: survives as a medium cost-accuracy defect.*

> Recursion (`partial`) survives as a *facet of Gap #2*: `delegates_to`/`dependencies` give
> delegation, but there is no Crawfish-owned self-referential invocation bounded by a termination
> predicate — unbounded depth lives in the backend sub-agent model, not a guarded control structure.

**Skeptic's most-damning true statement:** *"The control plane is structurally absent, not
merely unshipped. The one control structure that most defines wrapping deterministic, guarded
code around a stochastic leaf — bounded iterate-until-goal — cannot be expressed as a Crawfish
artifact at all. When hand-rolled, the framework's guarantees evaporate. Crawfish is a
deterministic data-plane language for agents but only a fixed-topology pipeline for control flow;
calling it a 'programming language for agents' overstates it on the exact axis the thesis is
built to win."*

---

## 4. What's genuinely distinctive

Be fair: these are constructs **no other agent framework treats as language primitives**, and
they are real and enforced in code (Researcher-cited where comparative).

- **`Flow` as an info-flow/taint type on every `Parameter`.** FLUID (untrusted per-item session
  data, the injection boundary) vs STATIC (trusted), with taint propagated *through*
  context-summarization/compaction (a tainted summary stays tainted; untrusted data never becomes
  trusted). DSPy/LangGraph have nothing comparable; closest prior art is FlowCaml/Jif (full IF
  lattices) — Crawfish is a coarse two-point lattice, at parity in concept, behind in formal
  expressiveness, but **ahead of every agent framework**.
- **Reproducible content-addressed runs + replay cassettes as a first-class runtime.**
  `RecordReplayRuntime` replays bit-for-bit with the provider never calling a model — replay
  *for reproducibility*, distinct from Temporal/Inngest crash-recovery replay.
- **Cost-as-gas.** `CostBudget` threaded through every `RunContext`, charged per agent call
  (including each escalate tier), hard-killing on breach. Cost is an enforced runtime resource
  bound on the leaf, not just observability. (Behind formal gas/resource *typing*, which is a
  static guarantee — but the thesis claim is runtime gas, which is exactly what ships.)
- **Structural type compatibility** via `crawfish.typesystem` (ADR-0002) rather than name
  equality — a PL-grade compatibility relation on node IO that no comparable framework enforces.
- **Determinism by expressive restriction.** Ahead of DSPy (refuses the learned compiler) and
  LangGraph (refuses cyclic, data-dependent global control flow), and sharper than classical
  effect systems in collapsing *all* stochasticity into one typed primitive. The honest caveat:
  this lead comes from restriction, not from a richer type system than the PL prior art.

---

## 5. Path to a usable agent language (prioritized)

Framing (per the project's North Star): a Crawfish project should be an **exportable,
self-contained app** — not one pipeline — and the construct surface below doubles as the
**emission-target spec for craw code**. Every gap here is a place craw code (the planned Claude
Code fork/plugin generator) would otherwise have to synthesize brittle glue instead of emitting a
framework primitive. The two flagships are evaluated first.

### Must-have

1. **`Refine` / `Until` — the iterate-until-goal operator** *(flagship; ~1–2 wks)*. A first-class
   node: re-run a Definition until a typed stop condition holds — a `Classifier`/judge label, a
   `Rubric` clearing a threshold, or a predicate over the typed `Output` — guarded by `max_iters`
   + a shared `CostBudget` + no-progress/"stuck" detection. Must carry `Output` lineage + taint
   across iterations, charge every iteration to one budget, and checkpoint each turn to the Store.
   *Closes Gaps #1, #3. Serves: quality, cost, determinism. This is the headline feature.*
2. **An executable, cyclic-capable composition surface** *(flagship; ~2–3 wks)*. Make `Router` a
   runnable step and add a representation with back-edges (a typed graph or a recursion/loop node)
   so branch-then-continue and branch-then-recurse are Crawfish artifacts — with the *same*
   `check_types` assembly check, per-step Store checkpointing, and budget/cancel threading the
   linear `Workflow` already gets. *Closes Gap #2 and the recursion facet. This is what turns
   "pipeline" into "program/app."*

> **The CLI is craw code's interface.** An agent generator drives a system through a composable,
> observable command surface — not by importing the SDK (the same way Claude Code works through
> the shell). `craw` already spans author/package/operate/observe (`init`,`dev`,`test` ·
> `freeze`,`install`,`build`,`publish`,`export` · `run`,`deploy`,`manage`,`_supervise` ·
> `inspect`,`logs`,`visualize`), and `craw manage` (`cli.py:431`, `manage.py`) is the model to
> replicate: a Store-reading, live-state-controlling, fully scriptable command. The gap: **the
> entire optimization plane is libraries with no CLI** — `eval.py`/`tuner.py`/`learning.py` ship
> no `craw eval` / `craw tune` / `craw refine`. That is exactly the plane the self-optimizing
> mini-app must drive, so the new constructs below should each land as a `craw` subcommand, not
> just a Python class.

### Should-have

3. **Expose the optimization + control plane as `craw` subcommands** *(~1 wk on top of the
   library work)*. `craw eval` (run a Benchmark/GoldenSet, gate against baseline), `craw tune`
   (drive the Tuner), `craw refine` (the iterate-until-goal operator from #1), `craw learn`
   (trigger the LearningLoop promotion). Modelled on `craw manage`: Store-backed, scriptable,
   emission-emitting. *This is the surface craw code drives; serves the whole self-optimizing
   mini-app vision.*
4. **Bounded recursion primitive** — a Definition that invokes itself (or a sub-Definition) with
   a termination predicate + depth guard, distinct from backend sub-agent delegation. *Falls out
   of #2's back-edge representation; serves quality with a determinism guard.*
5. **Cost model that sees escalation/repair/retry** — give `estimate_cost` an
   escalation-probability / repair / retry-multiplier factor (or surface a worst-case interval) so
   the static estimate isn't a silent lower bound. *Closes Gap #5; serves cost.*
6. **Live single-flight / result memoization on the provider path** — collapse identical
   in-flight `(definition+inputs)` calls within a batch run, not just pre-recorded cassettes.
   *Closes Gap #4; serves cost, speed.*

### Nice-to-have

7. **Typed quorum/voting aggregator** — run N samples, take consensus (self-consistency). *Quality
   lever the aggregator layer could expose; currently `collect`/`count` only.*
8. **Dependency resolution + lockfile for Definition imports** — SemVer-range solver over
   `DefinitionRef` so a project's *dependency closure* is reproducible, not just a single frozen
   Definition. *Deepens determinism for the project-as-app unit.*
9. **A loop-aware static cost path** — once #1 exists, fold `max_iters × per-iteration` into
   `estimate_cost` so a refine loop previews a worst-case bound.

**Sequencing rationale:** #2 is the structural enabler — the back-edge representation is what
makes #1 a durable, type-checked, budget-bounded artifact rather than another runtime trick, and
#4 (recursion) falls out of it for free. Ship #1 first as the visible flagship if a quick win is needed (it
can land as a runtime-level operator), but #2 is what makes the "deterministic programming
language for agents" claim — and the exportable self-optimizing mini-app vision — actually true.

---

## 6. Vision — what the path unlocks

Two illustrative apps showing what programming in Crawfish looks like *once §5's must-haves land*.
Constructs that don't exist today are flagged **`← NEW`**. These are aspirational sketches, not
shipping API — they exist to make the payoff of the path concrete.

### 6.1 `ap-clerk` — an accounts-payable agent app

A self-contained Crawfish **project/app** (not one pipeline) that ingests purchase orders and
invoices, extracts line items, reconciles them, and either auto-posts to the ledger or routes a
mismatch to a human. Exportable, local-first, and continuously tuned by craw code.

```
ap-clerk/
  crawfish.toml            # the app manifest — this whole dir IS the deployable unit
  crawfish.lock            # resolved dependency closure (← NEW: real solver, §5.8)
  definitions/{extract,reconcile}/
  app.py                   # the PROGRAM — composition surface
  evals/golden.jsonl       # the gate
```

```python
import crawfish as cw

extract   = cw.load_definition("definitions/extract")
reconcile = cw.load_definition("definitions/reconcile")

# Typed quality gate: extraction is "good" when the schema validates AND
# subtotal+tax == grand_total to the cent. Pure, deterministic, no model call.
good_extraction = cw.Rubric(
    "extraction_ok",
    schema_conformance=cw.schema_conformance(LineItems),
    arithmetic=cw.numeric_tolerance("grand_total", "subtotal+tax", tol=0.01),
)

# ── THE HEADLINE CONSTRUCT: iterate-until-goal ──────────────────────
refine_extract = cw.Refine(                                    # ← NEW (§5.1)
    extract,
    until=good_extraction >= 0.95,
    max_iters=4,
    budget=cw.CostBudget(limit_usd=0.40),
    on_stuck="dead_letter",        # two no-progress iterations → quarantine
)

# ── EXECUTABLE BRANCHING: Router that actually runs its branches ────
review = cw.Router(                                            # ← NEW: runnable (§5.2)
    classifier=cw.Classifier.from_definition(
        reconcile, labels=["matched", "mismatch", "needs_human"], default="needs_human"
    ),
    branches={
        "matched":     ledger_sink,                            # auto-post
        "mismatch":    refine_extract.then(ledger_sink),       # re-extract, then post
        "needs_human": cw.ApprovalRequired(ledger_sink),       # durable idle on a gate
    },
)

# The app = a composition with a back-edge (refine) and a live branch — not a
# straight line. Same assembly-time type-check, per-step checkpoint, taint/
# lineage carry, and budget threading the linear Workflow gets today.
app = cw.Program(name="ap-clerk", version="2.0")               # ← NEW: cyclic-capable
app.source(invoice_inbox.fan_out())     # bulk: one Run per document
app.step(refine_extract)                # iterate-until-goal per item
app.step(review)                        # branch per item
```

```bash
craw run --budget 25.00                    # process today's inbox, hard $25 ceiling
craw eval evals/golden.jsonl               # ← NEW: gate against the golden set
craw refine definitions/extract --until extraction_ok>=0.95   # ← NEW: drive the loop ad-hoc
craw inspect <run-id>                      # the emission trace: every iteration, every $
craw export ap-clerk --to claude-code      # ship the whole app as a CC plugin
```

What the program guarantees that a hand-rolled Python loop can't today: the refine loop is
type-checked at assembly, **charges every iteration to one budget** (hard-stops at $0.40/doc),
**carries taint** (invoice text is FLUID — never an instruction, Sink target stays static-only),
and **checkpoints each iteration to the Store** so a crash resumes mid-refinement.

**The self-optimizing loop — craw code closes it.** As you work, craw code watches the emission
ledger and drives the CLI: it notices `extract` dead-letters 12% of multi-currency invoices
(cost spike on the tail), mines a few-shot example from the human-approved corrections, runs
`craw tune … --strategy fewshot` then `craw eval … --gate baseline`, and promotes the candidate
**only because it beats baseline on the golden set** (the LearningLoop gate). Generation is
stochastic; the **eval gate is the type-checker that makes self-modification safe**.

| Goal | What `ap-clerk` shows |
|---|---|
| **Determinism** | Only `extract`/`reconcile` model calls are stochastic; the Refine loop, Router, Rubric, arithmetic, budget, and promotion gate are pure. Replay reproduces the run bit-for-bit. |
| **Cost** | One `CostBudget` bounds the loop *by construction*; eval-gated tuning drives the expensive tail down; static `estimate_cost` models the `max_iters × per-iter` worst case (§5.9). |
| **Speed** | Bulk fan-out runs items concurrently under a shared budget; cheap-first refine exits early on clean invoices. |
| **Quality** | Output is *guaranteed* to satisfy a typed Rubric or be quarantined — not hoped to; craw code ratchets quality up over real usage. |

### 6.2 `house-guard` — validations that author themselves

A Crawfish app craw code installs into a repo and attaches to the *live coding loop*. It watches
what Claude edits and what gets corrected or rejected, then **writes its own validations** of the
project's implicit invariants and enforces them on every future change. The on-thesis move:
**learn validations stochastically, distill them into deterministic enforcement.**

Grounded in this repo's own rules (`CLAUDE.md`): every Store row carries `org_id`; no SDK import
in nodes; `Flow.FLUID` never reaches a system prompt. Today those live in a markdown file a human
hopes Claude reads — here they become learned, enforced Definitions.

```toml
# .crawfish/guard.toml — craw code drops this in; it hooks the edit/commit loop
[guard]
attach = ["PostToolUse:Edit", "PreCommit"]     # ← NEW: live coding-loop hooks
mode   = "shadow"                               # observe-only until a rule proves itself
```

```python
import crawfish as cw

guard  = cw.load_definition("definitions/invariant-guard")

# craw code mines corrections: every human revert / CI failure / review reject
# becomes a labeled example.
corpus = cw.GoldenSet.from_corrections(                        # ← NEW: corrections → eval data
    store, kinds=["human_revert", "ci_failure", "review_reject"]
)

# Propose a rule from the corpus, then DISTILL the learned signal into a pure
# predicate (an AST check) — learn stochastically, enforce deterministically.
candidate = cw.synthesize_validation(                          # ← NEW (the headline idea)
    name="store_calls_carry_org_id",
    from_examples=corpus.filter(rule_hint="tenancy"),
    distill_to="predicate",
)

# Before it can ever BLOCK Claude, it must earn it: graded against the corpus of
# past human decisions. Same LearningLoop gate — reversible, ceiling-bounded.
report = cw.gate_against_baseline(candidate, corpus, min_precision=0.98)
guard.add_validation(candidate, enforce="block" if report.promoted else "warn")
```

A validation is born in three stages, all on-thesis:

1. **Learn (stochastic, at the leaf):** craw code reads the corrections and *proposes* a rule —
   the only nondeterministic step.
2. **Distill (deterministic):** the rule becomes a pure predicate / AST check — a `Metric`
   returning 0/1 with zero model calls. Reproducible forever after.
3. **Earn enforcement (eval-gated):** it graduates from *warn* to *block* only if ≥98% precise
   against real past decisions. A flaky validator can never block your work.

```bash
craw guard status                 # ← NEW: which rules are shadow/warn/block, + precision
craw guard explain store_calls_carry_org_id   # examples it learned from, the AST check
craw guard promote <rule>         # optional human approval gate for graduation
craw eval evals/corpus.jsonl      # re-grade every learned rule against new corrections
```

```
house-guard ▸ shadow rule "fluid_input_not_in_system_prompt" agreed with 41/41
             past decisions → promoting to BLOCK.
house-guard ▸ blocked Edit: definitions/extract/agent.py imports the SDK directly
             (rule "no_sdk_import_in_nodes", precision 1.00, learned from PR #112 revert).
```

| Goal | What `house-guard` shows |
|---|---|
| **Determinism** | Learning is stochastic and isolated; enforcement is a distilled pure predicate that replays identically — a rule never blocks differently on the same diff twice. |
| **Quality** | Tacit invariants stop depending on a human remembering `CLAUDE.md` — caught at edit-time, with the example that justified the rule. |
| **Cost** | Distilling to predicates makes enforcement ~free (no standing judge per edit); the model is paid only when *learning* a new rule, rarely. |
| **Speed** | Mistakes are caught at edit-time, not at CI/review — the most expensive place to find them. |

**The deepest point:** the same eval gate that makes prompt-tuning safe makes self-written
validations safe. A validation that can block work is as dangerous as a code change that can — so
it earns the right to enforce by proving itself against real history, exactly the way a promoted
Definition does. Both apps are themselves Crawfish **apps** (exportable, versioned, replayable),
eval-gated by the **LearningLoop**, driven by craw code through **`craw` CLI** commands — so the
audit's three flagships (iterate-until-goal, executable composition, CLI-exposed optimization
plane) are precisely what make them possible.

---

## Appendix — research comparison (Researcher, cited)

| System | Dimension | Crawfish is | Source |
|---|---|---|---|
| DSPy | determinism of program around the call | **ahead** (call is the only stochastic primitive vs a learned compiler) | arxiv 2310.03714 |
| DSPy | iterate-until-goal (Assertions/Suggestions backtrack-retry) | **behind** | arxiv 2312.13382 |
| LangGraph | control-flow expressiveness (cycles, conditional edges) | **behind** | docs.langchain.com graph-api |
| LangGraph | determinism by construction | **ahead** (refuses data-dependent global control flow) | activewizards LangGraph |
| Temporal | deterministic-execution discipline | **parity** (quarantines nondeterminism into a leaf) | jack-vanlightly 2025-11 |
| Temporal/Inngest | durable crash-recovery replay | **behind** (replay is for reproducibility, not durability) | docs.temporal.io / inngest |
| PL theory | information-flow/taint typing | **parity in concept** (coarse 2-point lattice vs FlowCaml/Jif) | normalesup FlowCaml |
| PL theory | resource/gas typing | **behind** (runtime meter, not a static type) | arxiv 1801.01896 |
| PL theory | nondeterminism in one typed primitive | **ahead** (sharper than pervasive effect typing) | Cornell sm-jsac03 |
