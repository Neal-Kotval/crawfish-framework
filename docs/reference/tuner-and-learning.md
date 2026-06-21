# Tuner & learning

How an agent gets *better* at its job without a human editing it by hand, and without a
live model in the loop. The Tuner searches a space of candidate configurations and keeps
the one that scores best; the learning loop wraps that search in a safe, reversible
promotion policy. These live in `crawfish.tuner` and `crawfish.learning`.

**Symbols on this page:** `Mutation` · `Candidate` · `PromptMutator` ·
`PromptVariantMutator` · `KnobGridMutator` · `FewShotMutator` · `ChainMutator` ·
`SearchStrategy` · `TrialResult` · `TuneResult` · `Tuner` · `LearningLoop` ·
`PromotionOutcome` · `VersionRecord`

---

## Core

An agent in Crawfish is described by a **Definition** — its team of agents, each agent's
prompt, its model, and other settings. Those settings are the **knobs** you can turn:
which model runs, what the prompt says, which few-shot examples are attached. Tuning means
trying different knob settings and keeping the best one.

A **mutator** produces the things to try. Given a starting Definition (the **base**), a
mutator enumerates **candidates** — each candidate is a new Definition with some knobs
changed, plus a record of exactly what was changed (the **mutation**). Crucially a mutator
never asks a model to invent new text: it only selects and combines settings the author
already supplied. That keeps the search reproducible and keeps untrusted text off the
instruction path.

The **Tuner** runs the search. It scores the base, then scores each candidate against a
**Benchmark** (a fixed set of tasks plus a rubric that turns each run into numbers). It
keeps the best-scoring candidate — but only if that candidate actually beats the base and
does not score worse on any measured dimension. That last check is the **regression gate**:
a worse candidate is never chosen. The output of a full run is a **TuneResult**, and each
individual scored attempt is a **TrialResult**.

A search costs real money once a real model is wired in, so the Tuner enforces an **autonomy
ceiling** — three independent stops. It halts when a spend budget is exhausted, when a
cancel signal fires, or when a hard cap on the number of trials is reached. An autonomous
search can never run away.

The **LearningLoop** points the Tuner at an agent's *own* Definition and adds a **promotion
policy**: the winner becomes the agent's new active version only if it improves *and* clears
a stored quality bar (the **baseline**). Every version — the base and any promoted candidate
— is recorded as a frozen, content-hashed **VersionRecord** in the store, so a bad promotion
is fully reversible: you can roll back to any earlier version. The result of one improve
cycle is a **PromotionOutcome**.

---

## Ramps up

The Tuner's design — propose prompt variants and few-shot examples, search, keep the
benchmark-best, regression-gate the winner — is borrowed from DSPy's ideas while
deliberately rejecting the dependency. See
[ADR 0015](../architecture/decisions/0015-prompt-optimization-method-for-the-tuner.md) for the rationale.

### Mutators are pure and seeded

Every mutator subclasses `PromptMutator` and implements one method, `propose(base, *, seed)`,
which yields `Candidate`s. The contract is **purity**: no model call, no I/O, no wall clock,
no global random state. The same `base` plus the same `seed` must yield identical candidates
in identical order. Each mutator enforces this by **sorting** its inputs before enumerating —
so a Python `set` or `dict` iteration order never leaks into the proposal order.

`PromptVariantMutator` swaps or appends prompt text from an author-supplied pool. It sorts
and de-duplicates the variants, then in `mode="replace"` substitutes the primary agent's
`prompt`, or in `mode="append"` adds a `Prompt` to the Definition's `injected_prompts`
targeting that agent's role. With `include_base=True` (the default) it yields the unchanged
base first.

`KnobGridMutator` is a Cartesian product (`itertools.product`) over discrete typed knobs:
the primary agent's `model`, `context_strategy`, and `policies`; the team's `coordination`;
and a discretised `temperature` grid. Each axis is sorted before the product. `AgentSpec`
has no `temperature` field, so `temperature` rides in the `Mutation`'s audit trail (`knobs`)
rather than being written onto the spec.

`FewShotMutator` injects few-shot exemplars chosen from a golden set of `EvalCase`s. It sorts
the cases by id, then for each of `samples` runs derives a seeded subset of size `k` (seed +
sample index), renders the picks as one static `Prompt` block, and appends it. The seed
governs *which* `k` cases are chosen, so the choice is reproducible.

`ChainMutator` concatenates several mutators' proposals in declared order — the way you
combine, say, a prompt sweep with a knob grid in one search.

The "primary agent" the knobs apply to is the team **lead** if one is set, otherwise the
first agent. Tuning a Definition with no agents raises `ValueError`.

### Each candidate is a fresh frozen artifact

A frozen Definition rejects mutation, so the search never edits in place. For every candidate
the mutator builds a new Definition and **re-freezes** it with a fresh content-hash version:
the model is serialised (minus its volatile `version` field), hashed, and that hash becomes
the new `version.sha`. Two structurally-identical candidates collapse to the same sha
(idempotent); any knob difference produces a distinct sha. This matters for determinism: when
a real model is replayed from a recorded cassette, the cassette key varies on the Definition's
version, so distinct candidates never collide on replay.

### How the search decides

`Tuner.tune` scores the base first (the bar to beat). For each candidate it computes the score
deltas against the current running best. A candidate is **accepted** when it strictly improves
on at least one dimension and is no worse than `tolerance` on every dimension (`beats_best`),
*and* it is not a regression versus the base within `tolerance` (`clean_vs_base`). On
acceptance it becomes the new best. The winner is therefore never worse than the base.

`SearchStrategy` controls the order in which candidates are tried — never *which* candidates
exist (the mutator owns that). `GRID` keeps the mutator's proposal order. `RANDOM` takes a
seeded sample of `sample_size` candidates (a fixed seed gives a fixed sample). `EVOLUTIONARY`
is a seeded shuffle — a reproducible reordering of the full pool.

### The autonomy ceiling

Before scoring *anything*, `tune` checks the ceiling: an already-cancelled or
already-exhausted context returns the base unscored with an empty trial log, so the search
never starts past its ceiling. Inside the loop the order is: stop at `max_trials`; check the
cancel token then the budget; then charge `cost_per_trial_usd` *before* scoring (a trial costs
even on a replay hit). `CostBudget.charge` raises `BudgetExceeded` past the hard limit. The
`stopped_reason` on the result records which bound fired: `"exhausted"`, `"budget"`,
`"cancelled"`, or `"max_trials"`. The loop is never bounded by wall clock.

### The promotion gate

`LearningLoop.improve` is a thin policy over `Tuner.tune`. It runs the search, records the
tuned-from base as a frozen `VersionRecord` (seeding the regression baseline on first use),
then decides:

- A ceiling breach (`budget` / `cancelled` / `max_trials`) **with no improvement** is never a
  promotion — outcome reason `"ceiling:<reason>"`.
- If the Tuner found nothing better than the base — reason `"no_improvement"`.
- If the winner regresses against the *stored* baseline (via `gate_against_baseline`) — reason
  `"gated"`. The baseline is the separate, persisted quality bar; it survives restarts and is
  what stops a noisy candidate from silently replacing a working agent.
- Otherwise the winner is **promoted**: recorded as a frozen `VersionRecord`, marked the single
  active version, and the baseline advances to its scores.

The loop mutates only static configuration (through the pure mutators), so a promotion can
never introduce a fluid — untrusted, per-item — sink target, and untrusted content can never
drive a promotion.

`rollback(sha)` re-activates any prior recorded version and resets the baseline to that
version's scores, so subsequent cycles are gated against the version actually in force. An
unknown `sha` raises `KeyError`.

---

## API reference

### `Mutation`

`class Mutation(BaseModel)` — the typed knob change that produced a candidate (the audit
trail).

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `kind` | `str` | — (required) | The mutator family, e.g. `"knob_grid"`, `"prompt_variant"`, `"few_shot"`. |
| `label` | `str` | — (required) | Short stable id for the change, e.g. `"variant[0]"`, `"model=fast"`. |
| `knobs` | `dict[str, JSONValue]` | `{}` | The concrete settings applied. |

### `Candidate`

`class Candidate(BaseModel)` — a proposed point in the knob space plus the patch that produced
it. Allows arbitrary types.

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `definition` | `Definition` | — (required) | A mutated, re-frozen Definition with a distinct version sha. |
| `mutation` | `Mutation` | — (required) | The typed knob change. |

### `PromptMutator`

`class PromptMutator(ABC)` — deterministically enumerate candidate Definitions from a base.

```python
@abstractmethod
def propose(self, base: Definition, *, seed: int) -> Iterator[Candidate]
```

Pure: no model calls, I/O, wall clock, or global RNG. Same `base` + `seed` ⇒ identical
candidates in identical order.

### `PromptVariantMutator`

`class PromptVariantMutator(PromptMutator)` — swap/append from an author-supplied pool of
prompt variants.

```python
def __init__(
    self,
    variants: Sequence[str],
    *,
    mode: str = "replace",
    include_base: bool = True,
) -> None
```

`mode` must be `"replace"` or `"append"` (else `ValueError`). Variants are sorted and
de-duplicated. `mode="replace"` substitutes the primary agent's `prompt`; `mode="append"`
adds a `Prompt` to `injected_prompts`. `include_base=True` yields the unchanged base first.

### `KnobGridMutator`

`class KnobGridMutator(PromptMutator)` — Cartesian product over discrete typed knobs.

```python
def __init__(
    self,
    *,
    models: Sequence[str] | None = None,
    context_strategies: Sequence[str | None] | None = None,
    policies: Sequence[list[str]] | None = None,
    coordination: Sequence[Coordination] | None = None,
    temperature: Sequence[float] | None = None,
) -> None
```

Each supplied axis is sorted before the product. `model` / `context_strategy` / `policies`
land on the primary agent; `coordination` lands on the team; `temperature` travels only in the
`Mutation` audit trail. With no axes supplied, `propose` yields nothing.

### `FewShotMutator`

`class FewShotMutator(PromptMutator)` — inject few-shot exemplars from a golden set.

```python
def __init__(self, cases: Sequence[EvalCase], *, k: int = 2, samples: int = 1) -> None
```

`k < 1` or `samples < 1` raises `ValueError`. Cases are sorted by id; `k` is clamped to the
number of cases. Each of `samples` runs derives a seeded subset of `k` cases (seed + sample
index) and appends it as one static `Prompt`. Empty `cases` yields nothing.

### `ChainMutator`

`class ChainMutator(PromptMutator)` — concatenate several mutators' proposals in declared
order.

```python
def __init__(self, mutators: Sequence[PromptMutator]) -> None
```

### `SearchStrategy`

`class SearchStrategy(str, Enum)` — the order candidates are tried (not which exist).

| Member | Value | Meaning |
| --- | --- | --- |
| `SearchStrategy.GRID` | `"grid"` | Exhaustive enumeration in the mutator's proposal order. |
| `SearchStrategy.RANDOM` | `"random"` | Seeded sample of `sample_size` candidates (fixed seed → fixed sample). |
| `SearchStrategy.EVOLUTIONARY` | `"evolutionary"` | Seeded shuffle — a reproducible reordering of the full pool. |

### `TrialResult`

`class TrialResult(BaseModel)` — one scored trial (the ordered audit log). Allows arbitrary
types.

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `index` | `int` | — (required) | Position in the trial order. |
| `mutation` | `Mutation` | — (required) | The knob change scored in this trial. |
| `version` | `str` | — (required) | The candidate's `str(Version)` (`major.minor-sha`). |
| `scores` | `dict[str, float]` | — (required) | Benchmark scores for this candidate. |
| `accepted` | `bool` | — (required) | True iff it beat the running best and was regression-clean. |

### `TuneResult`

`class TuneResult(BaseModel)` — the outcome of a tune. Allows arbitrary types.

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `best` | `Definition` | — (required) | The winning frozen Definition (the base if nothing beat it). |
| `best_scores` | `dict[str, float]` | — (required) | The winner's benchmark scores. |
| `base_scores` | `dict[str, float]` | — (required) | The base's benchmark scores (the bar to beat). |
| `improved` | `bool` | — (required) | True iff the winner beats the base and is regression-clean. |
| `trials` | `list[TrialResult]` | — (required) | The ordered trial log. |
| `stopped_reason` | `str` | — (required) | `"exhausted"` \| `"budget"` \| `"cancelled"` \| `"max_trials"`. |

### `Tuner`

`class Tuner` — deterministic search over a mutator's candidates, scored by a Benchmark.

```python
def __init__(
    self,
    benchmark: Benchmark,
    mutator: PromptMutator,
    *,
    strategy: SearchStrategy = SearchStrategy.GRID,
    max_trials: int = 64,
    sample_size: int | None = None,
    tolerance: float = 0.0,
    cost_per_trial_usd: float = 0.0,
    emit_progress: bool = False,
    pipeline: str | None = None,
) -> None
```

`max_trials < 1` raises `ValueError`. `tolerance` is the per-dimension slack allowed before a
delta counts as a regression. `cost_per_trial_usd` is charged against `ctx.cost_budget` per
trial. `emit_progress` emits per-trial `METRIC` emissions (lazily imported, failures swallowed
so a search never breaks).

```python
async def tune(
    self,
    base: Definition,
    ctx: RunContext,
    runtime: AgentRuntime,
    *,
    seed: int = 0,
) -> TuneResult
```

Scores the base, then each candidate in strategy order; returns the regression-gated
benchmark-best. The autonomy ceiling is checked before any scoring. The only model contact is
`Benchmark.run`, which is replay-deterministic.

### `VersionRecord`

`class VersionRecord(BaseModel)` — one frozen, auditable point in an agent's version lineage,
persisted through the `Store`. Allows arbitrary types.

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `agent` | `str` | — (required) | The learning-loop name this version belongs to. |
| `sha` | `str` | — (required) | The candidate's content-hash version sha (the lineage key). |
| `version` | `str` | — (required) | Human-readable `str(Version)` (`major.minor-sha`). |
| `definition` | `Definition` | — (required) | The frozen Definition at this point. |
| `scores` | `dict[str, float]` | — (required) | The benchmark scores that justified this version. |
| `role` | `str` | — (required) | `"base"` \| `"promoted"`. |
| `parent_sha` | `str \| None` | `None` | The version this one was derived from (lineage edge). |
| `active` | `bool` | `False` | True iff this is the agent's currently-active version. |

### `PromotionOutcome`

`class PromotionOutcome(BaseModel)` — the result of one `improve` cycle. Allows arbitrary
types.

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `promoted` | `bool` | — (required) | True iff a strictly-better, gate-clean candidate replaced the active one. |
| `reason` | `str` | — (required) | `"promoted"` \| `"no_improvement"` \| `"gated"` \| `"ceiling:<reason>"`. |
| `active` | `Definition` | — (required) | The active Definition after this cycle (base if not promoted). |
| `base_sha` | `str` | — (required) | The frozen version tuned from (the lineage parent). |
| `candidate_sha` | `str` | — (required) | The Tuner's winning version (== `base_sha` if nothing better). |
| `base_scores` | `dict[str, float]` | — (required) | The base's scores. |
| `candidate_scores` | `dict[str, float]` | — (required) | The winner's scores. |
| `tune` | `TuneResult` | — (required) | The full Tuner trial log (the search audit trail). |

### `LearningLoop`

`class LearningLoop` — a self-improving agent: the Tuner plus an eval-gated, versioned
promotion policy.

```python
def __init__(
    self,
    name: str,
    tuner: Tuner,
    store: Store,
    *,
    org_id: str = "local",
    tolerance: float = 0.0,
) -> None
```

```python
async def improve(
    self,
    base: Definition,
    ctx: RunContext,
    runtime: AgentRuntime,
    *,
    seed: int = 0,
) -> PromotionOutcome
```

Runs one eval-gated self-versioning cycle: delegates the search to `Tuner.tune`, then promotes
the winner only if it improved *and* passes the stored baseline. Same `base` + `seed` ⇒ same
outcome.

Other methods: `history() -> list[VersionRecord]` (the full lineage); `active() -> VersionRecord
| None` (the currently-active record); `rollback(sha) -> Definition` (re-activate a prior
version and reset the baseline to its scores; raises `KeyError` if `sha` is unknown).

---

## Example

A deterministic tune: a `KnobGridMutator` sweeps the agent's `model` knob, scored by a fixed
function (`slow`→1, `mid`→5, `fast`→9) through a `MockRuntime` — no live model. The grid sorts
the models alphabetically, so `fast` is tried first and accepted as the best.

```python
import asyncio
import os
import tempfile

from crawfish.batch import Task
from crawfish.core.context import CostBudget, RunContext
from crawfish.core.types import Flow, Parameter
from crawfish.definition.types import AgentSpec, Definition, TeamSpec
from crawfish.metrics import Benchmark, OutputNumber, Rubric
from crawfish.runtime.base import RunRequest
from crawfish.runtime.mock import MockRuntime
from crawfish.runtime.prompt import pick_agent
from crawfish.store import SqliteStore
from crawfish.tuner import KnobGridMutator, Tuner


# A fixed deterministic scorer: the score depends only on the `model` knob.
def responder(request: RunRequest) -> str:
    agent = pick_agent(request.definition, request.role)
    return str({"slow": 1, "mid": 5, "fast": 9}.get(agent.model or "", 0))


base = Definition(
    team=TeamSpec(agents=[AgentSpec(role="worker", prompt="do the thing", model="slow")]),
    inputs=[Parameter(name="task", type="text", flow=Flow.FLUID)],
)
benchmark = Benchmark(Rubric([OutputNumber(name="score")]), [Task(description="a"), Task(description="b")])
tuner = Tuner(benchmark, KnobGridMutator(models=["slow", "mid", "fast"]))

with tempfile.TemporaryDirectory() as d:
    store = SqliteStore(os.path.join(d, "t.db"))
    ctx = RunContext(store=store, cost_budget=CostBudget(limit_usd=None))
    result = asyncio.run(tuner.tune(base, ctx, MockRuntime(responder), seed=0))

print("improved:", result.improved)
print("best model:", result.best.team.agents[0].model)
print("base score:", result.base_scores["score"])
print("best score:", result.best_scores["score"])
print("stopped:", result.stopped_reason)
for t in result.trials:
    print(f"  trial {t.index}: {t.mutation.label!r} score={t.scores['score']} accepted={t.accepted}")
```

??? success "▶ Output"

    ```text
    improved: True
    best model: fast
    base score: 1.0
    best score: 9.0
    stopped: exhausted
      trial 0: 'model=fast' score=9.0 accepted=True
      trial 1: 'model=mid' score=5.0 accepted=False
      trial 2: 'model=slow' score=1.0 accepted=False
    ```
