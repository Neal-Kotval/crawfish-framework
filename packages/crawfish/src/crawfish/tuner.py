"""The Tuner — deterministic in-house search over Definition knobs (ADR 0015).

CRA-176. Improve a :class:`~crawfish.definition.types.Definition`'s ``AgentSpec``
knobs against a :class:`~crawfish.metrics.Benchmark`, *without* any new dependency and
*without* any live model call inside the search. The decision is binding (ADR 0015):
borrow DSPy's ideas (propose prompt variants + few-shot, search, keep the
benchmark-best, regression-gate the winner) and reject the dependency.

Two halves:

* :class:`PromptMutator` — a **pure, seeded** enumerator of candidate Definitions from a
  base. NO model call, no I/O, no wall clock, no global RNG: same ``base`` + same
  ``seed`` ⇒ identical candidates in identical order. Concrete mutators
  (:class:`PromptVariantMutator`, :class:`KnobGridMutator`, :class:`FewShotMutator`)
  operate on the *typed knobs*, never inventing free text via a model.
* :class:`Tuner` — a deterministic search loop. For each candidate (in deterministic
  order) it re-freezes the mutated Definition (a frozen artifact rejects mutation, so a
  fresh one is built), scores it via ``Benchmark.run`` under the injected
  ``AgentRuntime`` (the **only** model contact, already replay-deterministic), keeps the
  benchmark-best, and **regression-gates** the winner against the base so a worse
  candidate is never promoted.

Determinism (the load-bearing DoD): proposal order is pure Python; scoring goes through
the cassette, whose key varies on ``definition.version`` — and each candidate is a
distinct re-frozen Definition with a distinct content sha, so candidates never collide on
replay. The search is **bounded** by ``ctx.cost_budget`` / ``ctx.cancel_token`` /
``max_trials`` (an autonomy ceiling — security), never by wall clock.
"""

from __future__ import annotations

import hashlib
import json
import random
from abc import ABC, abstractmethod
from collections.abc import Iterator, Sequence
from enum import Enum

from pydantic import BaseModel, Field

from crawfish.core.context import RunContext
from crawfish.core.types import JSONValue
from crawfish.definition.types import AgentSpec, Coordination, Definition, Prompt
from crawfish.eval import EvalCase
from crawfish.metrics import Benchmark, compare, is_regression
from crawfish.runtime.base import AgentRuntime
from crawfish.versioning.version import Version

__all__ = [
    "Mutation",
    "Candidate",
    "PromptMutator",
    "PromptVariantMutator",
    "KnobGridMutator",
    "FewShotMutator",
    "ChainMutator",
    "SearchStrategy",
    "TrialResult",
    "TuneResult",
    "Tuner",
]


# -- typed knob change + candidate ------------------------------------------
class Mutation(BaseModel):
    """The typed knob change that produced a candidate (the audit trail).

    ``kind`` names the mutator family; ``knobs`` carries the concrete settings applied
    (e.g. ``{"model": "fast", "temperature": 0.3}``) so a trial log is fully explainable
    without re-deriving it. ``label`` is a short stable id for the change.
    """

    kind: str
    label: str
    knobs: dict[str, JSONValue] = Field(default_factory=dict)


class Candidate(BaseModel):
    """A proposed point in the knob space + the patch that produced it (ADR 0015)."""

    model_config = {"arbitrary_types_allowed": True}

    definition: Definition  # a mutated, re-frozen Definition (distinct version sha)
    mutation: Mutation  # the typed knob change


# -- helpers: re-freeze a mutated Definition with a fresh content sha --------
def _content_sha(definition: Definition) -> str:
    """A deterministic content hash over the Definition's knob-bearing payload.

    Mirrors the versioning intent (re-freezing a changed Definition yields a new sha):
    serialise the model (minus the volatile ``version`` field) canonically and hash it,
    so two structurally-identical candidates collapse to the same sha and two different
    ones diverge — which in turn gives them distinct cassette keys on replay.
    """
    payload = definition.model_dump(mode="json")
    payload.pop("version", None)
    blob = json.dumps(payload, sort_keys=True, default=str).encode()
    return hashlib.sha256(blob).hexdigest()[:12]


def _refreeze(base: Definition, mutated: Definition) -> Definition:
    """Return a frozen copy of ``mutated`` carrying a fresh content-hash version.

    A frozen artifact rejects mutation, so the search never edits in place: each
    candidate is a new, sealed Definition. The new ``version.sha`` is the content hash,
    so a candidate that differs from the base in any knob gets a distinct version and
    therefore a distinct cassette key (no replay collision); an identical candidate
    re-hashes to the same sha (idempotent).
    """
    sha = _content_sha(mutated)
    version = Version(major=base.version.major, minor=base.version.minor, sha=sha)
    # model_copy keeps a fresh (unfrozen) Version object we can seal without touching base.
    candidate = mutated.model_copy(update={"version": version}, deep=True)
    candidate.freeze()
    return candidate


def _with_agents(base: Definition, agents: Sequence[AgentSpec]) -> Definition:
    """A deep, *unfrozen* copy of ``base`` with its team agents replaced."""
    team = base.team.model_copy(update={"agents": list(agents)}, deep=True)
    return base.model_copy(update={"team": team, "version": Version()}, deep=True)


def _primary_agent(definition: Definition) -> AgentSpec:
    """The agent the knobs apply to (lead if set, else the first)."""
    if definition.team.lead:
        lead = definition.agent(definition.team.lead)
        if lead is not None:
            return lead
    if not definition.team.agents:
        raise ValueError("cannot tune a Definition with no agents")
    return definition.team.agents[0]


# -- the mutator contract (PURE + seeded) -----------------------------------
class PromptMutator(ABC):
    """Deterministically enumerate candidate Definitions from a base one (ADR 0015).

    PURE: no model calls, no I/O, no wall-clock/global RNG. Given the same base
    Definition and the same ``seed``, :meth:`propose` MUST yield identical candidates in
    identical order. This is the determinism contract the DoD requires.
    """

    @abstractmethod
    def propose(self, base: Definition, *, seed: int) -> Iterator[Candidate]:
        """Yield candidate Definitions (each re-frozen) in a deterministic order."""


class PromptVariantMutator(PromptMutator):
    """Swap/append from an **author-supplied, static** pool of prompt variants.

    The prompt text is *data the author provides* — the Tuner only selects/combines it,
    never invents it via a model (that keeps the mutator pure and keeps untrusted/fluid
    text off the instruction path, per SECURITY.md). ``mode='replace'`` substitutes the
    primary agent's ``prompt``; ``mode='append'`` adds a :class:`Prompt` to
    ``injected_prompts`` targeting that agent's role.
    """

    def __init__(
        self,
        variants: Sequence[str],
        *,
        mode: str = "replace",
        include_base: bool = True,
    ) -> None:
        if mode not in ("replace", "append"):
            raise ValueError(f"mode must be 'replace' or 'append', got {mode!r}")
        # Sort for a stable, set/dict-order-free enumeration; de-dup preserving order.
        seen: set[str] = set()
        self.variants: list[str] = []
        for v in sorted(variants):
            if v not in seen:
                seen.add(v)
                self.variants.append(v)
        self.mode = mode
        self.include_base = include_base

    def propose(self, base: Definition, *, seed: int) -> Iterator[Candidate]:
        if self.include_base:
            yield Candidate(
                definition=_refreeze(base, base.model_copy(deep=True)),
                mutation=Mutation(kind="prompt_variant", label="base", knobs={}),
            )
        agent = _primary_agent(base)
        for i, text in enumerate(self.variants):
            if self.mode == "replace":
                new_agent = agent.model_copy(update={"prompt": text}, deep=True)
                agents = [new_agent if a.role == agent.role else a for a in base.team.agents]
                mutated = _with_agents(base, agents)
            else:
                mutated = base.model_copy(
                    update={
                        "injected_prompts": [
                            *base.injected_prompts,
                            Prompt(target=agent.role, text=text),
                        ],
                        "version": Version(),
                    },
                    deep=True,
                )
            yield Candidate(
                definition=_refreeze(base, mutated),
                mutation=Mutation(
                    kind="prompt_variant",
                    label=f"variant[{i}]",
                    knobs={"mode": self.mode, "prompt": text},
                ),
            )


class KnobGridMutator(PromptMutator):
    """Cartesian product over discrete typed knobs (``itertools.product`` semantics).

    Enumerates the primary agent's ``model`` / ``context_strategy`` / ``policies`` (a
    subset list), the team ``coordination``, and a **discretised** ``temperature`` grid.
    Pure and deterministic: every axis is sorted before the product, so no ``set``/``dict``
    iteration order leaks into proposal order. ``AgentSpec`` carries no temperature field,
    so ``temperature`` travels in the Mutation audit trail (and, if a runtime consumes it,
    via the candidate's injected config) rather than being written onto the spec.
    """

    def __init__(
        self,
        *,
        models: Sequence[str] | None = None,
        context_strategies: Sequence[str | None] | None = None,
        policies: Sequence[list[str]] | None = None,
        coordination: Sequence[Coordination] | None = None,
        temperature: Sequence[float] | None = None,
    ) -> None:
        self.models = sorted(models) if models else None
        self.context_strategies = (
            sorted(context_strategies, key=lambda s: (s is not None, s or ""))
            if context_strategies
            else None
        )
        # Each policy choice is a list; sort the choices by their JSON form for stability.
        self.policies = (
            sorted((list(p) for p in policies), key=lambda p: json.dumps(p, sort_keys=True))
            if policies
            else None
        )
        self.coordination = sorted(coordination, key=lambda c: c.value) if coordination else None
        self.temperature = sorted(temperature) if temperature else None

    def _axes(self) -> list[tuple[str, list[JSONValue]]]:
        axes: list[tuple[str, list[JSONValue]]] = []
        if self.models is not None:
            axes.append(("model", list(self.models)))
        if self.context_strategies is not None:
            axes.append(("context_strategy", list(self.context_strategies)))
        if self.policies is not None:
            axes.append(("policies", [list(p) for p in self.policies]))
        if self.coordination is not None:
            axes.append(("coordination", [c.value for c in self.coordination]))
        if self.temperature is not None:
            axes.append(("temperature", list(self.temperature)))
        return axes

    def propose(self, base: Definition, *, seed: int) -> Iterator[Candidate]:
        import itertools

        axes = self._axes()
        if not axes:
            return
        names = [name for name, _ in axes]
        value_lists = [values for _, values in axes]
        agent = _primary_agent(base)
        for combo in itertools.product(*value_lists):
            knobs: dict[str, JSONValue] = dict(zip(names, combo, strict=True))
            agent_updates: dict[str, JSONValue] = {}
            if "model" in knobs:
                agent_updates["model"] = knobs["model"]
            if "context_strategy" in knobs:
                agent_updates["context_strategy"] = knobs["context_strategy"]
            if "policies" in knobs:
                agent_updates["policies"] = knobs["policies"]
            new_agent = agent.model_copy(update=agent_updates, deep=True)
            agents = [new_agent if a.role == agent.role else a for a in base.team.agents]
            mutated = _with_agents(base, agents)
            if "coordination" in knobs:
                mutated.team.coordination = Coordination(knobs["coordination"])
            label = ",".join(f"{k}={knobs[k]}" for k in names)
            yield Candidate(
                definition=_refreeze(base, mutated),
                mutation=Mutation(kind="knob_grid", label=label, knobs=knobs),
            )


class FewShotMutator(PromptMutator):
    """Inject few-shot exemplars selected deterministically from a golden set.

    DSPy's bootstrap idea, made pure: sort the cases by id, take a seeded subset of size
    ``k``, and inject them as a single **static** :class:`Prompt` block targeting the
    primary agent's role. No model call — the exemplars are author/golden data, selected
    not invented. The seed governs *which* k of the sorted cases are chosen, so the choice
    is reproducible.
    """

    def __init__(self, cases: Sequence[EvalCase], *, k: int = 2, samples: int = 1) -> None:
        if k < 1:
            raise ValueError("k must be >= 1")
        if samples < 1:
            raise ValueError("samples must be >= 1")
        # Sort by case id so iteration order is stable and set/dict-order-free.
        self.cases = sorted(cases, key=lambda c: c.id)
        self.k = min(k, len(self.cases))
        self.samples = samples

    def _render(self, picks: Sequence[EvalCase]) -> str:
        lines = ["Examples:"]
        for case in picks:
            inp = json.dumps(case.inputs, sort_keys=True)
            out = json.dumps(case.label if case.label is not None else case.output, sort_keys=True)
            lines.append(f"- input={inp} -> output={out}")
        return "\n".join(lines)

    def propose(self, base: Definition, *, seed: int) -> Iterator[Candidate]:
        if not self.cases:
            return
        agent = _primary_agent(base)
        for s in range(self.samples):
            # Derive all randomness from the single passed seed (+ sample index).
            rng = random.Random(f"{seed}:{s}")
            picks = sorted(rng.sample(self.cases, self.k), key=lambda c: c.id)
            text = self._render(picks)
            mutated = base.model_copy(
                update={
                    "injected_prompts": [
                        *base.injected_prompts,
                        Prompt(target=agent.role, text=text),
                    ],
                    "version": Version(),
                },
                deep=True,
            )
            yield Candidate(
                definition=_refreeze(base, mutated),
                mutation=Mutation(
                    kind="few_shot",
                    label=f"fewshot[k={self.k},s={s}]",
                    knobs={"case_ids": [c.id for c in picks]},
                ),
            )


class ChainMutator(PromptMutator):
    """Concatenate several mutators' proposals in declared order (deterministic)."""

    def __init__(self, mutators: Sequence[PromptMutator]) -> None:
        self.mutators = list(mutators)

    def propose(self, base: Definition, *, seed: int) -> Iterator[Candidate]:
        for m in self.mutators:
            yield from m.propose(base, seed=seed)


# -- search strategy + results ----------------------------------------------
class SearchStrategy(str, Enum):
    GRID = "grid"  # exhaustive enumeration in proposal order
    RANDOM = "random"  # seeded sample of the proposals (fixed seed -> fixed sample)
    EVOLUTIONARY = "evolutionary"  # seeded shuffle, a reproducible reordering


class TrialResult(BaseModel):
    """One scored trial in the search (the ordered audit log)."""

    model_config = {"arbitrary_types_allowed": True}

    index: int
    mutation: Mutation
    version: str
    scores: dict[str, float]
    accepted: bool


class TuneResult(BaseModel):
    """The outcome of a tune: the winning Definition + the ordered trial log."""

    model_config = {"arbitrary_types_allowed": True}

    best: Definition
    best_scores: dict[str, float]
    base_scores: dict[str, float]
    improved: bool  # True iff the winner beats the base and is regression-clean
    trials: list[TrialResult]
    stopped_reason: str  # "exhausted" | "budget" | "cancelled" | "max_trials"


# -- the Tuner --------------------------------------------------------------
class Tuner:
    """Deterministic search over a mutator's candidates, scored by a Benchmark.

    The autonomy ceiling is load-bearing (a search can otherwise spend unbounded real
    cost): every trial is charged ``cost_per_trial_usd`` against ``ctx.cost_budget``, the
    loop stops when the budget is exhausted, the cancel token fires, or ``max_trials`` is
    hit. Determinism: same ``base`` + ``seed`` ⇒ identical winner AND identical trial
    order, because proposal order is pure and each candidate's cassette key is distinct
    (distinct re-frozen version sha).
    """

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
    ) -> None:
        if max_trials < 1:
            raise ValueError("max_trials must be >= 1")
        self.benchmark = benchmark
        self.mutator = mutator
        self.strategy = strategy
        self.max_trials = max_trials
        self.sample_size = sample_size
        self.tolerance = tolerance
        self.cost_per_trial_usd = cost_per_trial_usd
        self.emit_progress = emit_progress
        self.pipeline = pipeline

    def _ordered_candidates(self, base: Definition, *, seed: int) -> list[Candidate]:
        """Materialise candidates in the deterministic order the strategy dictates."""
        candidates = list(self.mutator.propose(base, seed=seed))
        if self.strategy is SearchStrategy.GRID:
            return candidates
        rng = random.Random(seed)
        if self.strategy is SearchStrategy.RANDOM:
            n = self.sample_size if self.sample_size is not None else len(candidates)
            n = min(n, len(candidates))
            # rng.sample over an explicitly-ordered list -> deterministic given the seed.
            return rng.sample(candidates, n)
        # EVOLUTIONARY: seeded shuffle (a reproducible reordering of the same pool).
        shuffled = list(candidates)
        rng.shuffle(shuffled)
        return shuffled

    def _budget_remaining(self, ctx: RunContext) -> bool:
        remaining = ctx.cost_budget.remaining_usd
        if remaining is None:
            return True
        # Stop once exhausted; if a per-trial cost is set, require room for the next trial.
        if remaining <= 0:
            return False
        return remaining >= self.cost_per_trial_usd

    def _ceiling_breached(self, ctx: RunContext) -> str | None:
        """The autonomy-ceiling reason to stop, or ``None`` to proceed.

        Cancel token first (a kill-switch), then the cost budget — the two bounds that
        keep an autonomous search from spending unbounded real-model cost.
        """
        if ctx.cancel_token.cancelled:
            return "cancelled"
        if not self._budget_remaining(ctx):
            return "budget"
        return None

    def _emit(self, ctx: RunContext, trial: TrialResult) -> None:
        """Emit tuning progress as typed METRIC Emissions, if the stream is available.

        Optional and defensive (ADR 0015 / spec: "don't break the stream"): the emission
        module is imported lazily and any failure is swallowed, so a build that predates
        the Emission contract — or a transient store error — never aborts the search.
        """
        if not self.emit_progress:
            return
        try:
            import importlib

            emission = importlib.import_module("crawfish.emission")
        except ImportError:
            return
        emission_cls = emission.Emission
        metric_kind = emission.EmissionKind.METRIC
        for name, value in trial.scores.items():
            e = emission_cls(
                kind=metric_kind,
                run_id=ctx.run_id,
                org_id=ctx.org_id,
                pipeline=self.pipeline,
                attrs={
                    "metric": f"tuner.{name}",
                    "value": value,
                    "trial": trial.index,
                    "version": trial.version,
                    "accepted": trial.accepted,
                },
            )
            try:
                emission.emit(ctx.store, e, org_id=ctx.org_id)
            except Exception:  # noqa: BLE001 — progress emission must never break the search
                pass

    async def tune(
        self,
        base: Definition,
        ctx: RunContext,
        runtime: AgentRuntime,
        *,
        seed: int = 0,
    ) -> TuneResult:
        """Search the candidate space; return the benchmark-best (regression-gated).

        The base is scored first (the bar to beat). Each candidate is scored via
        ``Benchmark.run`` (the only model contact, replay-deterministic). A candidate is
        accepted iff it strictly improves on the running best AND is not a regression vs
        the base. The winner is never worse than the base.

        The autonomy ceiling is checked **before** any scoring: an already-cancelled or
        already-exhausted context returns the base unscored (empty trial log) rather than
        burning a single benchmark run — the search never starts past its ceiling.
        """
        base_frozen = _refreeze(base, base.model_copy(deep=True))

        # Honour the ceiling up front so we never even score the base past it.
        ceiling = self._ceiling_breached(ctx)
        if ceiling is not None:
            return TuneResult(
                best=base_frozen,
                best_scores={},
                base_scores={},
                improved=False,
                trials=[],
                stopped_reason=ceiling,
            )

        base_scores = await self.benchmark.run(base_frozen, ctx, runtime)

        best = base_frozen
        best_scores = base_scores
        improved = False
        trials: list[TrialResult] = []
        stopped_reason = "exhausted"

        for index, candidate in enumerate(self._ordered_candidates(base, seed=seed)):
            if index >= self.max_trials:
                stopped_reason = "max_trials"
                break
            ceiling = self._ceiling_breached(ctx)
            if ceiling is not None:
                stopped_reason = ceiling
                break

            # Charge the autonomy-ceiling cost BEFORE scoring (a trial costs even on a
            # replay hit); CostBudget.charge raises BudgetExceeded past the hard limit.
            if self.cost_per_trial_usd:
                ctx.cost_budget.charge(self.cost_per_trial_usd)

            scores = await self.benchmark.run(candidate.definition, ctx, runtime)

            deltas = compare(best_scores, scores)
            beats_best = any(d > 0 for d in deltas.values()) and all(
                d >= -self.tolerance for d in deltas.values()
            )
            clean_vs_base = not is_regression(base_scores, scores, tolerance=self.tolerance)
            accepted = beats_best and clean_vs_base

            trial = TrialResult(
                index=index,
                mutation=candidate.mutation,
                version=str(candidate.definition.version),
                scores=scores,
                accepted=accepted,
            )
            trials.append(trial)
            self._emit(ctx, trial)

            if accepted:
                best = candidate.definition
                best_scores = scores
                improved = True

        return TuneResult(
            best=best,
            best_scores=best_scores,
            base_scores=base_scores,
            improved=improved,
            trials=trials,
            stopped_reason=stopped_reason,
        )
