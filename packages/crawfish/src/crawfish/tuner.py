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
import tomllib
from abc import ABC, abstractmethod
from collections.abc import Iterator, Mapping, Sequence
from enum import Enum

from pydantic import BaseModel, Field

from crawfish.core.context import RunContext
from crawfish.core.types import JSONValue
from crawfish.definition.types import AgentSpec, Coordination, Definition, Prompt
from crawfish.eval import EvalCase
from crawfish.metrics import Benchmark, compare, is_regression
from crawfish.runtime.base import AgentRuntime
from crawfish.versioning.version import FrozenError, Version

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
    # CRA-209 — two-axis mode unifier (per-knob tunable + train()/eval())
    "KnobDomain",
    "TuneSpec",
    "tune_spec_sha",
    "train",
    "eval",
    "guard_consequential",
    # CRA-213 — cost-regularized Objective
    "ObjectiveForm",
    "Objective",
    "ObjectiveScore",
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

    Delegates to the **canonical** :meth:`Definition.content_sha` (ADR 0017 / F-5), the
    single source of hash truth: it drops the volatile ``version`` and identity ``id``,
    and folds in the tunable decode knobs (hash-neutral when None). Two structurally
    identical candidates collapse to one sha; any knob change diverges — which gives each
    candidate a distinct cassette key on replay. The Tuner does not re-implement the law.
    """
    return definition.content_sha()


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


# == CRA-209 — the two-axis mode unifier ====================================
# PyTorch's hardest lesson: ``requires_grad`` and ``.eval()`` are *orthogonal* axes.
# Crawfish unifies them here.
#
#   * Axis 1 (tunable) — DATA. Which knobs may move is a :class:`TuneSpec`, content-hashed
#     into the Definition (authored as ``tune.toml``), not imperative code at a call site.
#   * Axis 2 (mode) — ``train()`` / ``eval()``. ``train(d)`` returns an *unfrozen* copy
#     (knobs may change, copy-on-write minting a fresh ``Version.sha``); ``eval(d)`` is the
#     frozen, reproducible artifact. Eval is the default for a loaded Definition.
#
# Load-bearing rule: a consequential Sink may fire, a run may be recorded, and a content
# hash is stable **only in eval mode**. A consequential side effect against an unfrozen
# (train-mode) Definition raises — see :func:`guard_consequential`.


# A scalar JSON leaf a knob may take. Kept narrow on purpose: a knob domain is static,
# author-supplied config (it enters the content hash), never a fluid/model-derived value.
KnobValue = str | int | float | bool | None


class KnobDomain(BaseModel):
    """One tunable knob: where it lives (``path``), its candidate ``values``, and whether
    the Tuner is *allowed* to move it (``tunable``).

    ``path`` is a dotted address into the Definition's knob space — the authoring vocabulary
    the mutators already speak: ``agent.<role>.prompt`` / ``.model`` / ``.temperature`` /
    ``.sample_k`` / ``.context_strategy`` / ``.policies``, ``team.coordination``,
    ``injected_prompts``. ``tunable=False`` pins the knob: it is declared (so its domain is
    documented and hashed) but :meth:`TuneSpec.named_knobs` will not yield it and a
    TuneSpec-driven mutator must refuse to move it.
    """

    model_config = {"frozen": True}

    path: str
    values: list[KnobValue] = Field(default_factory=list)
    tunable: bool = True


class TuneSpec(BaseModel):
    """Axis 1 as data: the set of knobs a Tuner may search, content-hashable + authorable.

    This is the typed form of ``tune.toml``. It is *static config* — it enters the
    Definition's content identity via :func:`tune_spec_sha` (the documented seam for folding
    it into ``Definition.tune``; see the changelog) so editing the search space changes the
    sha, exactly like editing any other knob. It carries **no** free model text and never
    reads a fluid value: the security boundary is upheld because a knob *domain* is author
    config, not session data.
    """

    model_config = {"frozen": True}

    knobs: list[KnobDomain] = Field(default_factory=list)

    def named_knobs(self) -> Iterator[tuple[str, KnobDomain]]:
        """Yield ``(path, domain)`` for every **tunable** knob, sorted by path.

        Pinned (``tunable=False``) knobs are skipped — they are declared but immovable.
        Path-sorted so enumeration is stable and free of dict/insertion-order leakage (the
        same determinism contract the mutators hold).
        """
        for domain in sorted(self.knobs, key=lambda k: k.path):
            if domain.tunable:
                yield domain.path, domain

    def is_tunable(self, path: str) -> bool:
        """True iff ``path`` is declared **and** tunable. Unknown paths are not tunable."""
        for domain in self.knobs:
            if domain.path == path:
                return domain.tunable
        return False

    # -- tune.toml round-trip ------------------------------------------------
    @classmethod
    def from_toml(cls, text: str) -> TuneSpec:
        """Parse a ``tune.toml`` document into a :class:`TuneSpec`.

        Authoring shape (array-of-tables, stable + diffable)::

            [[knob]]
            path = "agent.worker.model"
            values = ["fast", "mid", "slow"]
            tunable = true
        """
        data = tomllib.loads(text)
        raw = data.get("knob", [])
        knobs = [KnobDomain(**entry) for entry in raw] if isinstance(raw, list) else []
        return cls(knobs=knobs)

    def to_dict(self) -> dict[str, object]:
        """The canonical, JSON-ready payload (path-sorted) for export + hashing.

        Used by :func:`tune_spec_sha` and by ``Definition.export()`` once ``Definition.tune``
        is wired (the types/compiler follow-up): a stable dict that round-trips and whose
        edit perturbs the sha.
        """
        return {
            "knobs": [
                domain.model_dump(mode="json")
                for domain in sorted(self.knobs, key=lambda k: k.path)
            ]
        }


def tune_spec_sha(spec: TuneSpec) -> str:
    """Deterministic 12-char content hash of a :class:`TuneSpec`.

    The seam for folding the tune-spec into a Definition's content identity: combine
    ``Definition.content_sha()`` with ``tune_spec_sha(spec)`` (or, once ``Definition.tune``
    lands, hash them together) so editing the search space changes the sha. Empty spec
    hashes to a stable constant — adding an *empty* ``tune.toml`` is hash-neutral.
    """
    blob = json.dumps(spec.to_dict(), sort_keys=True, default=str).encode()
    return hashlib.sha256(blob).hexdigest()[:12]


# -- Axis 2: train() / eval() mode -------------------------------------------
def train(definition: Definition) -> Definition:
    """Enter **train mode**: return an *unfrozen* copy whose knobs may change (CRA-209).

    Mirrors PyTorch's ``.train()``. The returned Definition is mutable (``frozen is False``)
    with a **fresh** ``Version`` — so a training mutation is a copy-on-write that mints a new
    ``version.sha`` when re-frozen, never an in-place edit of the original frozen artifact.
    Consequential side effects are forbidden in this mode (:func:`guard_consequential`).

    Idempotent in spirit: ``eval(train(d))`` re-hashes to ``d``'s eval sha (see :func:`eval`).
    """
    return definition.model_copy(update={"version": Version()}, deep=True)


def eval(definition: Definition) -> Definition:  # noqa: A001 — mirrors torch.nn.Module.eval()
    """Enter **eval mode**: return the frozen, reproducible artifact (CRA-209).

    Mirrors PyTorch's ``.eval()`` and is the default for a loaded Definition. Re-freezes via
    the content-hash path: the returned Definition is frozen with ``version.sha`` set to its
    canonical :meth:`Definition.content_sha`, so ``eval(train(d))`` is idempotent — it hashes
    back to the same eval sha whenever the knobs are unchanged. Only in this mode may a
    consequential Sink fire or a run be recorded.
    """
    return _refreeze(definition, definition)


def guard_consequential(definition: Definition) -> None:
    """Raise unless ``definition`` is in eval mode (frozen) — the load-bearing rule.

    The single gate every consequential boundary calls before committing an irreversible
    side effect (a Sink write, a recorded run): a side effect against an unfrozen
    (train-mode) Definition is forbidden, because a training artifact has no stable content
    identity to key idempotency or attribute the effect to. Raises :class:`FrozenError`
    (the established "wrong mutability state" signal); against an eval-mode Definition it is
    a no-op.
    """
    if not definition.frozen:
        raise FrozenError(
            "consequential side effects (Sink writes, recorded runs) require an eval-mode "
            "(frozen) Definition; this one is in train mode — call eval(defn) first"
        )


# == CRA-213 — cost-regularized Objective ===================================
class ObjectiveForm(str, Enum):
    """How the :class:`Objective` scalarizes quality against cost."""

    LINEAR = "linear"  # Σ wᵢ·scoreᵢ − λ·cost − μ·ece  (weighted scalarization)
    EPSILON = "epsilon"  # minimize cost s.t. quality >= floor  (ε-constraint)


class ObjectiveScore(BaseModel):
    """The scalar an :class:`Objective` assigns a candidate, with its decomposition.

    ``value`` is what the Tuner ranks on (higher is better). The component fields make the
    decision explainable in the trial log: ``quality`` is the weighted score sum,
    ``cost_penalty`` is ``λ·cost`` (normalized), ``ece_penalty`` is ``μ·ece``. ``feasible``
    is the ε-constraint gate (always True in linear form).
    """

    model_config = {"frozen": True}

    value: float
    quality: float
    cost_penalty: float
    ece_penalty: float
    feasible: bool = True


class Objective(BaseModel):
    """Cost-regularized loss the Tuner maximizes among gate-passing candidates (CRA-213).

    ``value(scores, cost_usd=…, ece=…) = Σ wᵢ·scoreᵢ − λ·cost_term − μ·ece``. Pure arithmetic
    over **passed-in values**: ``cost_usd`` (from the deterministic :func:`estimate_cost`) and
    ``ece`` (from AL-T4's calibration metric — passed as a value; this module never imports
    ``calibrate``, keeping the two decoupled). Same inputs ⇒ same scalar.

    The cost term is **normalized** so ``λ`` is unit-free and portable: each candidate's cost
    is divided by ``cost_baseline_usd`` (set this to the cheapest candidate's cost, so the
    cheapest contributes a penalty of 1.0 and ``λ`` reads as "quality points I will trade for
    one cheapest-candidate's worth of spend"). With no baseline the raw dollar cost is used.

    The hard regression gate stays in the Tuner: this objective only **re-ranks** among
    candidates that already pass it, so it can never promote a quality regression.

    ``ObjectiveForm.EPSILON`` switches to the ε-constraint form — minimize cost subject to
    ``quality >= quality_floor`` — surfaced through ``feasible`` on the score.
    """

    model_config = {"frozen": True}

    weights: dict[str, float] = Field(default_factory=dict)
    cost_weight: float = 0.0  # λ
    ece_weight: float = 0.0  # μ (ships as 0 until calibrate lands; AL-T4)
    form: ObjectiveForm = ObjectiveForm.LINEAR
    quality_floor: float = 0.0  # ε-constraint: minimum acceptable Σ wᵢ·scoreᵢ
    cost_baseline_usd: float | None = None  # normalizer; None -> raw dollars

    def quality(self, scores: Mapping[str, float]) -> float:
        """The weighted quality sum ``Σ wᵢ·scoreᵢ``.

        A metric absent from ``weights`` defaults to weight ``1.0`` (so a bare objective with
        no weights sums every recorded metric — matching the Tuner's default "sum of scores"
        intuition). A weight with no matching score contributes nothing.
        """
        if not self.weights:
            return float(sum(scores.values()))
        return float(sum(self.weights.get(name, 1.0) * value for name, value in scores.items()))

    def _cost_term(self, cost_usd: float) -> float:
        """The normalized cost, relative to ``cost_baseline_usd`` when set."""
        if self.cost_baseline_usd is not None and self.cost_baseline_usd > 0.0:
            return cost_usd / self.cost_baseline_usd
        return cost_usd

    def score(
        self, scores: Mapping[str, float], *, cost_usd: float, ece: float = 0.0
    ) -> ObjectiveScore:
        """The full decomposed objective for one candidate (deterministic + pure)."""
        quality = self.quality(scores)
        cost_penalty = self.cost_weight * self._cost_term(cost_usd)
        ece_penalty = self.ece_weight * ece
        if self.form is ObjectiveForm.EPSILON:
            # Minimize cost subject to a quality floor: rank by negated cost so the cheapest
            # *feasible* candidate wins; infeasible candidates are pushed below every feasible
            # one (and still ece-penalized for a stable total order).
            feasible = quality >= self.quality_floor
            value = -self._cost_term(cost_usd) - ece_penalty
            if not feasible:
                value -= 1e9
            return ObjectiveScore(
                value=value,
                quality=quality,
                cost_penalty=cost_penalty,
                ece_penalty=ece_penalty,
                feasible=feasible,
            )
        value = quality - cost_penalty - ece_penalty
        return ObjectiveScore(
            value=value,
            quality=quality,
            cost_penalty=cost_penalty,
            ece_penalty=ece_penalty,
            feasible=True,
        )

    def value(self, scores: Mapping[str, float], *, cost_usd: float, ece: float = 0.0) -> float:
        """The scalar objective ``Σ wᵢ·scoreᵢ − λ·cost − μ·ece`` (the ranking key)."""
        return self.score(scores, cost_usd=cost_usd, ece=ece).value


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
    # CRA-213 — deterministic per-candidate cost and the objective scalar it was ranked on
    # (both ``None`` when no Objective is configured, so the legacy log shape is preserved).
    cost_usd: float | None = None
    objective_value: float | None = None


class TuneResult(BaseModel):
    """The outcome of a tune: the winning Definition + the ordered trial log."""

    model_config = {"arbitrary_types_allowed": True}

    best: Definition
    best_scores: dict[str, float]
    base_scores: dict[str, float]
    improved: bool  # True iff the winner beats the base and is regression-clean
    trials: list[TrialResult]
    stopped_reason: str  # "exhausted" | "budget" | "cancelled" | "max_trials"
    # CRA-213 — the regression-clean, non-dominated trial indices (the Pareto frontier).
    # Populated only when ``pareto=True``; empty otherwise.
    pareto_front: list[int] = Field(default_factory=list)


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
        objective: Objective | None = None,
        pareto: bool = False,
        objective_items: int = 1,
        emit_progress: bool = False,
        pipeline: str | None = None,
    ) -> None:
        if max_trials < 1:
            raise ValueError("max_trials must be >= 1")
        if objective_items < 0:
            raise ValueError("objective_items must be >= 0")
        self.benchmark = benchmark
        self.mutator = mutator
        self.strategy = strategy
        self.max_trials = max_trials
        self.sample_size = sample_size
        self.tolerance = tolerance
        self.cost_per_trial_usd = cost_per_trial_usd
        # CRA-213 — cost-regularized acceptance. ``objective`` re-ranks among candidates that
        # already pass the hard regression gate (it can never promote a quality regression).
        # ``pareto`` additionally requires a candidate be non-dominated (better on quality
        # AND no worse on cost, or vice-versa) vs the running best. ``objective_items`` is the
        # item count handed to ``estimate_cost`` for each candidate's deterministic cost.
        self.objective = objective
        self.pareto = pareto
        self.objective_items = objective_items
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

    def _candidate_cost(self, definition: Definition) -> float:
        """Deterministic per-candidate cost from the single cost owner (:func:`estimate_cost`).

        Imported lazily to keep ``tuner`` import-light and to make the dependency on the cost
        model explicit at the one site that needs it. Pure: no model call, no I/O.
        """
        from crawfish.cost import estimate_cost

        return estimate_cost(definition, items=self.objective_items).total_usd

    @staticmethod
    def _dominates(a_quality: float, a_cost: float, b_quality: float, b_cost: float) -> bool:
        """True iff candidate ``a`` Pareto-dominates ``b``: no worse on both axes (higher
        quality, lower cost) and strictly better on at least one."""
        no_worse = a_quality >= b_quality and a_cost <= b_cost
        strictly_better = a_quality > b_quality or a_cost < b_cost
        return no_worse and strictly_better

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

        # CRA-213 — cost-regularized acceptance state. The base is the bar to beat on the
        # objective too: its cost + objective value seed ``best_objective``. Only computed
        # when an objective (or pareto) is active, so the legacy quality path is untouched.
        cost_active = self.objective is not None or self.pareto
        best_cost = self._candidate_cost(best) if cost_active else 0.0
        best_objective = (
            self.objective.value(best_scores, cost_usd=best_cost)
            if self.objective is not None
            else 0.0
        )
        # Pareto frontier of regression-clean candidates: (index, quality, cost).
        front: list[tuple[int, float, float]] = []

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

            # The HARD gate is non-negotiable and identical in every mode: a regression vs the
            # base is never promoted. The cost term only re-ranks *among* gate-passers, so it
            # can never promote a quality regression (CRA-213).
            clean_vs_base = not is_regression(base_scores, scores, tolerance=self.tolerance)

            cand_cost: float | None = None
            cand_obj: float | None = None
            if self.objective is not None:
                cand_cost = self._candidate_cost(candidate.definition)
                cand_obj = self.objective.value(scores, cost_usd=cand_cost)
                quality = self.objective.quality(scores)
                # Accept iff the candidate passes the gate AND strictly improves the objective
                # over the running best. Pareto (when on) additionally requires non-domination
                # vs the running best — the gate refuses a dominated candidate.
                accepted = clean_vs_base and cand_obj > best_objective
                if accepted and self.pareto:
                    # Non-domination gate: the running best must not dominate the candidate.
                    best_quality = self.objective.quality(best_scores)
                    accepted = not self._dominates(best_quality, best_cost, quality, cand_cost)
            elif self.pareto:
                # Pure-Pareto mode (no scalar objective): accept a regression-clean candidate
                # that Pareto-dominates the running best on (quality, cost).
                cand_cost = self._candidate_cost(candidate.definition)
                quality = float(sum(scores.values()))
                best_quality = float(sum(best_scores.values()))
                accepted = clean_vs_base and self._dominates(
                    quality, cand_cost, best_quality, best_cost
                )
            else:
                # Legacy pure-quality acceptance (back-compat): strict per-metric improvement
                # over the running best, regression-clean vs the base.
                deltas = compare(best_scores, scores)
                beats_best = any(d > 0 for d in deltas.values()) and all(
                    d >= -self.tolerance for d in deltas.values()
                )
                accepted = beats_best and clean_vs_base

            trial = TrialResult(
                index=index,
                mutation=candidate.mutation,
                version=str(candidate.definition.version),
                scores=scores,
                accepted=accepted,
                cost_usd=cand_cost,
                objective_value=cand_obj,
            )
            trials.append(trial)
            self._emit(ctx, trial)

            # Maintain the regression-clean Pareto frontier for reporting (pareto mode only).
            if self.pareto and clean_vs_base and cand_cost is not None:
                quality_for_front = (
                    self.objective.quality(scores)
                    if self.objective is not None
                    else float(sum(scores.values()))
                )
                front = [
                    entry
                    for entry in front
                    if not self._dominates(quality_for_front, cand_cost, entry[1], entry[2])
                ]
                dominated = any(
                    self._dominates(q, c, quality_for_front, cand_cost) for _, q, c in front
                )
                if not dominated:
                    front.append((index, quality_for_front, cand_cost))

            if accepted:
                best = candidate.definition
                best_scores = scores
                improved = True
                if cost_active:
                    best_cost = (
                        cand_cost
                        if cand_cost is not None
                        else self._candidate_cost(candidate.definition)
                    )
                if self.objective is not None and cand_obj is not None:
                    best_objective = cand_obj

        return TuneResult(
            best=best,
            best_scores=best_scores,
            base_scores=base_scores,
            improved=improved,
            trials=trials,
            stopped_reason=stopped_reason,
            pareto_front=sorted(idx for idx, _, _ in front),
        )
