# Crawfish: a deterministic language **and** a tunable ML library for agents

> Design vision / future direction. Companion to the grounded audit in
> [`AGENT-LANGUAGE-AUDIT.md`](./AGENT-LANGUAGE-AUDIT.md), which rates what exists in `main`
> today. This doc is forward-looking: it consolidates the design ideas to seed a Linear
> "Agent Language" epic. Constructs that don't exist yet are flagged **`← NEW`**; everything
> else points at the construct it generalizes. Some pieces (Wiki/RAG) are explicitly deferrable.

---

## 0. The thesis, in two halves

Crawfish is **two things that share one substrate**:

- **Half A — a deterministic programming language for agents.** The agent call is the only
  stochastic primitive; control flow, types, effects, cost, memory, and concurrency around it
  are ordinary deterministic code. (This is the audited thesis.)
- **Half B — a tunable ML library for agents: "PyTorch, one level up, for LLMs."** An agent
  program is a composition of units with *learnable parameters* (prompts, few-shots, knobs,
  model choice, carry strategy). You define an objective (a `Rubric`), point an optimizer
  (`Tuner`/`LearningLoop`) at a dataset (`GoldenSet`), and the program *improves itself* —
  eval-gated, reversible, reproducible.

**The unifier (the key insight): the `mutable` property is `train`/`eval` mode.**
A Definition's tunable parameters are *mutable / `requires_grad=True`* in **train mode** (the
optimizer may change them) and *frozen / deterministic* in **eval mode** (inference replays
bit-for-bit). The same content-hash versioning that makes Half A reproducible is what lets
Half B checkpoint, roll back, and promote. The two halves are not bolted together — they are
the same property algebra seen from two sides.

```
            ┌──────────────────────── train mode ────────────────────────┐
            │  params mutable / requires_grad ; Tuner searches knob space  │
 Definition │  loss = Rubric over GoldenSet ; eval gate promotes a winner  │
 (a value)  └──────────────────────────────────────────────────────────────┘
            ┌──────────────────────── eval mode ─────────────────────────┐
            │  params frozen (content-hashed) ; deterministic ; replayable │
            └──────────────────────────────────────────────────────────────┘
```

---

## 1. Half A — the deterministic language

### 1.1 An operator library (the control plane)
The framework owns named, **guaranteed** control-flow operators; craw code chooses *which*,
*with what goal*, and *with what knobs*. The reason the framework must own them (not let craw
code hand-roll Python loops): only the framework-owned operator carries the
budget/checkpoint/taint/replay guarantees. Hand-rolled control flow loses all of them
(audit Gap #3).

```python
feature_loop(build, *, goal, max_iters=5, budget=None, critic=None,   # ← NEW
             carry="typed_fields", sample_k=1, temperature=0.3, on_stuck="dead_letter")
```

- `feature_loop` / `refine` — **iterate-until-goal**: re-run the leaf until a `Rubric`
  threshold / `Classifier` label / typed predicate holds, guarded by `max_iters` + `CostBudget`
  + no-progress detection. (Audit §5.1)
- `branch` — a **runnable `Router`** that executes its chosen branch in-program. (Audit §5.2;
  today `Router` only returns `(label, Node)` and isn't a runnable Workflow step.)
- `recurse` — a Definition invoking itself/sub-Definitions to a base case with a termination
  guard + depth bound. (Audit §5.4)
- These compose into a **`Program`** (`← NEW`, cyclic-capable) with the same assembly-time
  type-check, per-step Store checkpoint, and budget/cancel threading the linear `Workflow` has.

### 1.2 The property / capability algebra
Every object — data `Parameter`, `Definition`, `Wiki`, `Rag`, `Memory`, `Sink` — is a **typed
value with capability properties**, with object-level defaults that are **narrowable per
function call** (like `&` vs `&mut` / object capabilities). This *generalizes the `Flow`
effect system Crawfish already has* (its most distinctive feature per the audit) into a uniform
discipline.

| Property | data `Parameter` | `Wiki` | `Rag` | `Definition` | `Sink` | `Memory` |
|---|---|---|---|---|---|---|
| **trust** (static/fluid) | ✅ today | generalize | fluid (corpus is untrusted) | generalize | ✅ static-only target | generalize |
| **mutability** (frozen/mutable) | — | default+narrow | frozen by default | ✅ `Freezable` today | — | new |
| **taint** (propagates) | ✅ today | generalize | generalize (+hits tainted) | generalize | ✅ | generalize |
| **cost / capability** | — | — | per-query cost | ✅ routing tier | ✅ consent | — |

```python
feature_loop(build, summon=[ arch.readonly(), code.frozen() ])  # read-only wiki, pinned RAG
doc_writer(summon=[ arch.mutable() ])                            # this fn may write the wiki
```

**Determinism constraint (load-bearing):** *un-versioned mutability is forbidden.* A mutable
summoned object is read at a **pinned version** into each run (content-hashed snapshot);
mutation produces a *new* version and the run records which one it saw. This extends the
existing `Freezable` + `Version` machinery rather than breaking replay.

### 1.3 Definitions as composable, versioned variables
A `Definition` is itself a value with the property algebra — composable inside functions,
recallable, modifiable, resettable. Mental model: a mutable variable. Implementation:
a **persistent (immutable) data structure with version pointers — like git.**

```python
base = cw.load_definition("feature-impl")
augmented = (base
    .with_skill(cw.Skill("tdd-discipline"))                    # ← NEW: append a skill
    .with_context(cw.Wiki("repo/architecture").readonly())     # ← NEW: append context
    .with_agent(cw.AgentSpec("test-writer", delegates=True)))  # ← NEW: append a sub-agent
cw.save(augmented, as_="feature-impl+tdd"); later = cw.recall("feature-impl+tdd")  # ← NEW
v2 = augmented.with_skill(cw.Skill("perf-review"))             # modify → new sha
back = augmented.reset()                                        # ← reset = checkout base
```

- **modify** = `.with_*` returns a new Definition (new content hash); base untouched
  (copy-on-write — never in-place on a shared object, so concurrent runs can't corrupt it).
- **reset** = move the pointer to a pinned earlier version (checkout, not destruction).
- **recall** = resolve a name → a specific versioned value from the Registry/Store.
- Grounding: `Definition` already carries `agents` (`AgentSpec.delegates_to`), `Prompt`s,
  `MCPConnection`s, `dependencies` (`DefinitionRef`); ccexport models `ClaudeCodeSkill`.

### 1.4 Summonable units — Wiki & RAG *(deferrable)*
Two distinct knowledge primitives, both new, both summonable by reference:
- **`Wiki`** — a curated, structured, *authored* knowledge space (sections, editable entries).
  Maps today to `Memory` + `Context` made first-class.
- **`Rag`** — a *pure-retrieval* surface over an indexed corpus (embeddings/semantic search).
  Naturally `Flow.FLUID`; its hits inherit taint. `.frozen()` pins the index for reproducibility.

Design the **property algebra before the object kinds**, so each new kind just declares which
properties it supports.

---

## 2. Half B — the tunable ML library ("PyTorch for LLMs, one level up")

### 2.1 The analogy (and what maps to what)

| PyTorch | Crawfish | Status |
|---|---|---|
| `nn.Module` | `Definition` (composable unit with parameters) | exists |
| `nn.Parameter` (learnable weight) | a **knob**: prompt text, few-shots, model id, temperature, carry strategy, `max_iters` | exists (typed knobs) |
| `requires_grad` | the **`tunable`** property on a knob (which params the optimizer may touch) | **← NEW** |
| `forward()` | `Run.execute()` (the stochastic leaf) | exists |
| loss function | a **`Rubric`/`Metric`** (objective to maximize/minimize) | exists |
| autograd / `backward()` | eval-gated **search** (no gradient — propose mutation, score, gate) | exists (Tuner) |
| optimizer (SGD/Adam) | `SearchStrategy` (grid / evolutionary / few-shot / chain mutators) | exists |
| `optimizer.step()` | `LearningLoop` promote-if-beats-baseline (reversible, ceiling-bounded) | exists |
| `Dataset` / `DataLoader` | `GoldenSet` / `Source` + `Batch` | exists |
| `model.train()` / `model.eval()` | **`mutable`/`frozen`** mode on a Definition | **← NEW (the unifier)** |
| `state_dict()` / `load_state_dict()` | **`Definition.state_dict()` / `load_state()`** | **← NEW (see 2.2)** |
| checkpoint | `Freeze` + content-hash `Version` | exists |
| hyperparameters | meta-knobs (`max_iters`, `budget`, `sample_k`) | exists |
| model calibration | **variance / reliability measurement** over repeated runs | **← NEW (see 2.3)** |

The optimization loop is **"evolutionary backprop"**: no gradients, but a real
propose → evaluate → gate → promote cycle, where the eval gate is the type-checker that makes
self-modification safe (the same gate that makes `house-guard`'s self-written validations safe).

### 2.2 Loading definition variables into models *(the thing you asked for)*
A Definition's tunable state is a **state dict** — a typed bundle of its knobs (prompt,
few-shots, model, temperature, carry strategy, summoned units). Make it first-class so craw code
can load/save/swap/share parameter sets cleanly, exactly like `model.load_state_dict()`:

```python
state = tuned.state_dict()              # ← NEW: {prompt, fewshots, model, temp, carry, summons}
cw.save_state(state, "extract.v7")      # persist a tuned parameter set

fresh = cw.load_definition("extract")
fresh.load_state("extract.v7")          # ← NEW: bind a tuned param set into a fresh Definition
fresh.load_state(state, only=["fewshots", "temp"])   # partial load / parameter transfer
```

This gives: **parameter transfer** (carry few-shots learned on one task to another), **A/B of
state dicts**, **sharing tuned params across a fleet of Definitions**, and a clean separation of
*architecture* (the Definition shape) from *weights* (its state dict) — exactly PyTorch's split.
Each state dict is content-hashed, so loading one is reproducible.

### 2.3 Calibration & variance — measuring the stochasticity
To *tame* stochasticity you must first *measure* it. A new primitive runs a Definition N times
(or over a GoldenSet) and reports its reliability — the agent analog of model calibration:

```python
report = cw.calibrate(extract, golden, runs=5)   # ← NEW
report.output_variance     # how much do outputs differ run-to-run? (0 = deterministic)
report.rubric_mean, report.rubric_std            # quality + its spread
report.confidence_ece      # are self-reported confidences calibrated to actual accuracy?
report.abstention_rate     # how often does it (correctly) decline?
```

This turns "is this agent reliable?" from vibes into a number the Tuner can optimize against and
the eval gate can require.

### 2.4 Exploration vs exploitation *(and craw code's role)*
Any optimizer over the knob space faces the classic trade-off: **explore** new variants (might
be better, costs budget, may regress) vs **exploit** the current known-best (safe, cheap, but
never improves). Crawfish should make this an explicit, bounded dial rather than an accident:

- An **explore-rate** knob on the `SearchStrategy` (ε-greedy / UCB / Thompson-style), so a fixed
  fraction of runs trial candidates while the rest serve the promoted best. Bounded by
  `CostBudget` — exploration spends gas, so it's a budgeted resource like everything else.
- The **eval gate is the exploitation safety net**: an explored variant only *graduates* if it
  beats baseline, so exploration can never degrade production quality — it can only fail safely
  and cost some budget.

**craw code owns this balance over time.** In the future, craw code is the long-running agent
that decides *when* to explore (a new model dropped, quality drifted, a new corpus arrived) vs
*exploit* (deadline, tight budget, stable task) — adjusting the explore-rate per project from the
emission history, and driving it through `craw tune` / `craw learn`. The framework provides the
bounded, eval-gated mechanism; **craw code provides the policy.** (Same division as everywhere:
framework owns the guaranteed mechanism, craw code owns the stochastic decision.)

---

## 3. Taming the stochastic nature of agents — the technique menu

A catalogue of levers, each a construct/operator. The strategy is: **measure variance (2.3),
then apply the cheapest lever that drives it down, bounded by cost.**

| Technique | What it does | Status |
|---|---|---|
| **Iterate-until-goal** (`feature_loop`) | bounded refinement until a typed goal holds | ← NEW (audit §5.1) |
| **Typed output validation + repair** | schema forces structure; one bounded re-prompt on failure | ✅ today |
| **Self-consistency / quorum** | run N samples, take majority/consensus → cuts variance | ← NEW (`sample_k`, typed quorum, audit §5.7) |
| **Ensembles** | run several Definitions/models, aggregate their outputs | ← NEW |
| **Confidence gating / escalation** | cheap model first; escalate the unsure tail to a strong one | ✅ today (`EscalatingRuntime`) |
| **Abstention** | let the agent decline rather than hallucinate; route to human gate | partial (`ApprovalRequired`) → make first-class |
| **Critic / verifier loop** | a separate Definition checks the output (generator/discriminator) | ← NEW (`critic=` in `feature_loop`) |
| **Decomposition** | break a task into smaller typed steps, each lower-variance | ✅ (composition surface, deepen w/ §1.1) |
| **Decoding control** | temperature / top-p as tunable knobs; lower temp = more determinism | exists as a knob |
| **Constrained generation** | force outputs into a schema/grammar at decode time | ← NEW (provider-dependent) |
| **Replay / memoization** | identical calls collapse to a recorded, deterministic result | ✅ today (cassettes) + ← NEW live single-flight (audit §5.6) |
| **Seeding** | pin a seed where the backend supports it | partial |
| **Cost-regularized tuning** | penalize cost/complexity in the objective (the regularizer) | ← NEW (objective term) |
| **Calibration gating** | require a min reliability/ECE before a Definition may run unattended | ← NEW (2.3) |
| **Guardrail assertions** | invariants (`Rubric`/`Metric`) that must hold on every output | ✅ today |
| **Learned validations** | mine corrections → distill to deterministic predicates, eval-gated to block | ← NEW (`house-guard`) |

The through-line: **push every bit of nondeterminism toward a leaf you can measure, bound, and
gate — then let the deterministic program around it stay deterministic.**

---

## 4. What we still need (the build list)

Consolidated `← NEW` constructs, grouped. (Maps onto / extends the audit's §5 path.)

**Half A — language / control plane**
1. `feature_loop` / `refine` — iterate-until-goal operator *(flagship)*
2. Runnable `branch` + a cyclic-capable `Program` composition surface *(flagship)*
3. `recurse` — bounded self-referential Definition
4. The **property/capability algebra** (trust/mutability/taint/cost; narrow-per-call; versioned)
5. Definition-as-variable ops: `with_skill/with_context/with_agent`, `save`/`recall`,
   `modify`/`reset` (copy-on-write, content-hashed)
6. `Wiki` and `Rag` summonable knowledge objects *(deferrable)*

**Half B — tunable ML library**
7. `train`/`eval` mode + `requires_grad`-style `tunable` flag on knobs *(the unifier)*
8. `Definition.state_dict()` / `load_state()` — load tuned variables into models, parameter transfer
9. `cw.calibrate()` — variance / reliability / confidence-calibration measurement
10. A formal objective abstraction (Rubric-as-loss, with a cost-regularization term)
11. Self-consistency / quorum + ensemble + critic operators

**Plane / surface (from the audit, enabling both)**
12. CLI exposure of the optimization plane: `craw refine` / `eval` / `tune` / `learn` / `guard`
13. Cost model that sees escalation/repair/retry; live single-flight caching
14. Dependency resolver + lockfile for summoned units (reproducible closure)

---

## 5. Proposed Linear epic shape

**Epic: "Agent Language" — Crawfish as a deterministic, tunable agent language.**

- **Milestone 1 — Control plane (flagships):** #1 `feature_loop`, #2 runnable branch + `Program`.
  Closes audit Gaps #1–#3. *Highest leverage; everything else composes on these.*
- **Milestone 2 — The ML library half:** #7 train/eval mode, #8 state_dict/load_state, #9
  calibrate, #11 quorum/ensemble/critic. *Turns it into "PyTorch for LLMs."*
- **Milestone 3 — The property algebra & values:** #4 capability properties, #5
  Definition-as-variable, #3 recurse. *The uniform model; design the algebra before new kinds.*
- **Milestone 4 — Surfaces & accuracy:** #12 CLI, #13 cost/cache fixes, #14 resolver/lockfile.
- **Deferred:** #6 Wiki/RAG objects, #10 formal objective/regularizer, constrained decoding.

**Open questions to resolve in the ticket:**
- How does a mutable summoned object's version get pinned into a run's identity without bloating
  the content hash? (Snapshot ref vs full embed.)
- Is `recurse` a distinct operator or just `feature_loop` over a Definition that summons itself?
- Where does decoding-level control (temperature/seed/grammar) live — knob on the Definition, or
  property on the runtime call?
- Does `state_dict` include summoned-unit *references* (pinned versions) or just the prompt-level
  knobs? (Probably references-by-version, to stay reproducible.)
