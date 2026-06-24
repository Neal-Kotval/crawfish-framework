"""Learning agents — eval-gated self-versioning (CRA-177).

An agent that improves its OWN instructions/knobs over time, *safely*. This is the
:class:`~crawfish.tuner.Tuner` (CRA-176) pointed at an agent's own Definition, with the
winner **promotion-gated** against a stored regression baseline and the whole base →
candidate → promoted lineage recorded as content-hashed
:class:`~crawfish.versioning.version.Version`\\ s so a bad promotion is fully **reversible**.

The composition (we do NOT re-implement search):

* :meth:`LearningLoop.improve` calls ``Tuner.tune`` to search the agent's own knob space
  (reusing the Tuner's mutators, deterministic order, regression-gate-vs-base, and the
  autonomy ceiling — ``cost_budget`` / ``cancel_token`` / ``max_trials``). The Tuner is the
  *engine*; the loop adds the *promotion policy* on top of its winner.
* The Tuner's winner is promoted ONLY if it (a) actually improved on the base in this run
  AND (b) passes :func:`~crawfish.eval.gate_against_baseline` — no regression vs the stored
  baseline. A regression is never promoted; the active version is unchanged.
* Every version in the lineage (the base, the promoted candidate) is a frozen, content-hashed
  artifact persisted through the ``Store``. :meth:`LearningLoop.rollback` re-activates any
  prior recorded version — promotion is reversible by construction.

Safety (load-bearing):

* Promotion is **eval-gated** — :func:`gate_against_baseline` + the Tuner's own
  ``is_regression`` guard, so a noisy/worse candidate can never silently replace a working
  agent.
* The loop is bounded by the **autonomy ceiling** it inherits from the Tuner
  (``cost_budget`` exhaustion / ``cancel_token`` / ``max_trials``); a ceiling breach returns
  a non-promoting outcome rather than spending unbounded model cost.
* A promoted version is **frozen + auditable** (recorded with its scores + provenance)
  before it becomes the active version.
* The loop mutates only STATIC Definition config (via the Tuner's pure mutators, which never
  invent text via a model). It can never cross the static/fluid boundary, so a promotion can
  never introduce a fluid Sink target; untrusted (fluid) content can never drive a promotion.

Determinism: ``improve`` is a thin policy over ``Tuner.tune`` — same ``base`` + ``seed`` ⇒
identical winner, identical baseline scores ⇒ identical promotion decision.
"""

from __future__ import annotations

import hashlib
import json
from enum import Enum

from pydantic import BaseModel, Field

from crawfish.core.context import RunContext
from crawfish.definition.types import (
    DECODE_KNOB_FIELDS,
    AgentSpec,
    Coordination,
    Definition,
    DefinitionRef,
    Prompt,
)
from crawfish.eval import gate_against_baseline, load_baseline, save_baseline
from crawfish.runtime.base import AgentRuntime
from crawfish.store.base import Store
from crawfish.tuner import Tuner, TuneResult, _refreeze

__all__ = [
    "VersionRecord",
    "PromotionOutcome",
    "LearningLoop",
    # CRA-210 — AL-T2: the architecture/weights split (references-by-version transfer).
    "IncompatibleStateError",
    "RoleKnobs",
    "StateDict",
    "state_dict",
    "load_state",
    # CRA-214 — AL-T6: the explore-rate dial.
    "ExploreStrategy",
    "ExploreSchedule",
    "GraduationVerdict",
    "ServingDecision",
    "ServingLoop",
]


class VersionRecord(BaseModel):
    """One frozen, auditable point in an agent's version lineage.

    Persisted through the ``Store`` so the base → candidate → promoted history survives a
    process restart and a bad promotion can be rolled back to any prior ``sha``.
    """

    model_config = {"arbitrary_types_allowed": True}

    agent: str  # the learning-loop name this version belongs to
    sha: str  # the candidate's content-hash version sha (the lineage key)
    version: str  # the human-readable ``str(Version)`` (``major.minor-sha``)
    definition: Definition  # the frozen Definition at this point
    scores: dict[str, float]  # the benchmark scores that justified this version
    role: str  # "base" | "promoted"
    parent_sha: str | None = None  # the version this one was derived from (lineage edge)
    active: bool = False  # True iff this is the agent's currently-active version


class PromotionOutcome(BaseModel):
    """The result of one :meth:`LearningLoop.improve` cycle (the audit record)."""

    model_config = {"arbitrary_types_allowed": True}

    promoted: bool  # True iff a strictly-better, gate-clean candidate replaced the active one
    reason: str  # "promoted" | "no_improvement" | "gated" | "ceiling:<reason>"
    active: Definition  # the agent's active Definition AFTER this cycle (base if not promoted)
    base_sha: str  # the frozen version we tuned from (the lineage parent)
    candidate_sha: str  # the Tuner's winning version (== base_sha if it found nothing better)
    base_scores: dict[str, float]
    candidate_scores: dict[str, float]
    tune: TuneResult  # the full Tuner trial log (the search audit trail)


class LearningLoop:
    """A self-improving agent: the Tuner + an eval-gated, versioned promotion policy.

    The loop owns one named lineage of an agent's Definitions in the ``Store``. Each
    :meth:`improve` runs the Tuner over the *active* Definition's own knobs, then promotes
    the winner only if it beats the baseline (regression-gated). Promotion is recorded as a
    new frozen ``VersionRecord``; :meth:`rollback` re-activates any prior one.
    """

    def __init__(
        self,
        name: str,
        tuner: Tuner,
        store: Store,
        *,
        org_id: str = "local",
        tolerance: float = 0.0,
    ) -> None:
        self.name = name
        self.tuner = tuner
        self.store = store
        self.org_id = org_id
        self.tolerance = tolerance

    # -- lineage persistence (Store-backed, reversible) ---------------------
    @property
    def _kind(self) -> str:
        return f"learning:{self.name}"

    @property
    def _baseline_name(self) -> str:
        return f"learning:{self.name}"

    def _record(self, rec: VersionRecord) -> None:
        self.store.put_record(self._kind, rec.sha, rec.model_dump(mode="json"), org_id=self.org_id)

    def _get(self, sha: str) -> VersionRecord | None:
        raw = self.store.get_record(self._kind, sha, org_id=self.org_id)
        return None if raw is None else VersionRecord.model_validate(raw)

    def history(self) -> list[VersionRecord]:
        """The full version lineage for this agent (the recorded set of versions)."""
        return [
            VersionRecord.model_validate(r)
            for r in self.store.list_records(self._kind, org_id=self.org_id)
        ]

    def active(self) -> VersionRecord | None:
        """The agent's currently-active version record, if any has been recorded."""
        for rec in self.history():
            if rec.active:
                return rec
        return None

    def _set_active(self, sha: str) -> None:
        """Flip the active flag to ``sha`` (exactly one active version at a time)."""
        for rec in self.history():
            want = rec.sha == sha
            if rec.active != want:
                rec.active = want
                self._record(rec)

    def _record_base(self, base: Definition, base_scores: dict[str, float]) -> str:
        """Record the tuned-from base as a frozen version; return its content sha.

        The Tuner re-freezes the base internally; we reconstruct that same frozen artifact
        here (via the Tuner's own ``_refreeze``) so the lineage edge (``parent_sha``) points
        at a real, retrievable version a rollback can return to. Idempotent: a base already
        in the lineage is not re-written. Also seeds the regression baseline on first use.
        """
        base_frozen = _refreeze(base, base.model_copy(deep=True))
        base_sha = str(base_frozen.version.sha or "")
        if self._get(base_sha) is None:
            self._record(
                VersionRecord(
                    agent=self.name,
                    sha=base_sha,
                    version=str(base_frozen.version),
                    definition=base_frozen,
                    scores=base_scores,
                    role="base",
                    parent_sha=None,
                    active=self.active() is None,  # active only if the lineage was empty
                )
            )
        if load_baseline(self.store, self._baseline_name, org_id=self.org_id) is None:
            save_baseline(self.store, self._baseline_name, base_scores, org_id=self.org_id)
        return base_sha

    def _not_promoted(self, reason: str, parent_sha: str, result: TuneResult) -> PromotionOutcome:
        active = self.active()
        return PromotionOutcome(
            promoted=False,
            reason=reason,
            active=active.definition if active is not None else result.best,
            base_sha=parent_sha,
            candidate_sha=str(result.best.version.sha or ""),
            base_scores=result.base_scores,
            candidate_scores=result.best_scores,
            tune=result,
        )

    # -- the learning cycle -------------------------------------------------
    async def improve(
        self,
        base: Definition,
        ctx: RunContext,
        runtime: AgentRuntime,
        *,
        seed: int = 0,
    ) -> PromotionOutcome:
        """Run one eval-gated self-versioning cycle over ``base``'s own knobs.

        Delegates the search to ``Tuner.tune`` (inheriting its mutators, determinism and
        autonomy ceiling), then applies the promotion policy: promote the winner ONLY if it
        improved in this run AND passes the stored regression baseline. On promotion, the new
        frozen version is recorded + activated and the baseline advances; otherwise the active
        version is untouched. Same ``base`` + ``seed`` ⇒ same outcome.
        """
        result = await self.tuner.tune(base, ctx, runtime, seed=seed)

        # Persist the tuned-from base so the lineage is complete + the baseline is seeded.
        parent_sha = self._record_base(base, result.base_scores)

        # -- autonomy ceiling: a ceiling breach with no improvement is never a promotion --
        if result.stopped_reason in ("budget", "cancelled", "max_trials") and not result.improved:
            return self._not_promoted(f"ceiling:{result.stopped_reason}", parent_sha, result)

        # -- the Tuner found nothing better than the base ------------------
        if not result.improved:
            return self._not_promoted("no_improvement", parent_sha, result)

        # -- eval gate: never promote a regression vs the stored baseline ---
        cand = result.best
        cand_sha = str(cand.version.sha or "")
        cand_scores = result.best_scores
        gate_clean = gate_against_baseline(
            self.store,
            self._baseline_name,
            cand_scores,
            tolerance=self.tolerance,
            org_id=self.org_id,
        )
        if not gate_clean:
            return self._not_promoted("gated", parent_sha, result)

        # -- promote: record the frozen candidate, activate it, advance the baseline ------
        self._record(
            VersionRecord(
                agent=self.name,
                sha=cand_sha,
                version=str(cand.version),
                definition=cand,  # already frozen by the Tuner's _refreeze
                scores=cand_scores,
                role="promoted",
                parent_sha=parent_sha,
                active=False,  # _set_active flips exactly one active flag
            )
        )
        self._set_active(cand_sha)
        save_baseline(self.store, self._baseline_name, cand_scores, org_id=self.org_id)

        return PromotionOutcome(
            promoted=True,
            reason="promoted",
            active=cand,
            base_sha=parent_sha,
            candidate_sha=cand_sha,
            base_scores=result.base_scores,
            candidate_scores=cand_scores,
            tune=result,
        )

    # -- reversibility ------------------------------------------------------
    def rollback(self, sha: str) -> Definition:
        """Re-activate a prior recorded version (reverse a promotion).

        Returns the now-active frozen Definition. The regression baseline is reset to the
        rolled-back version's scores so subsequent ``improve`` cycles are gated against the
        version actually in force. Raises ``KeyError`` if ``sha`` is not in the lineage.
        """
        rec = self._get(sha)
        if rec is None:
            raise KeyError(f"no version {sha!r} in lineage for agent {self.name!r}")
        self._set_active(sha)
        save_baseline(self.store, self._baseline_name, rec.scores, org_id=self.org_id)
        return rec.definition


# ===========================================================================
# CRA-210 — AL-T2: state_dict() / load_state() (R5, "Hugging-Face-for-agent-weights")
# ---------------------------------------------------------------------------
# The architecture/weights split. A Tuner/LearningLoop winner is an opaque whole-Definition
# blob; you cannot transfer what it *learned* onto a sibling Definition of the same shape,
# share it across a fleet, or A/B two knob settings on one architecture. ``state_dict`` is
# the split: it serializes the **tunable knobs only** (the "weights") — per-role prompt /
# decode knobs / model / context_strategy / policies, ``injected_prompts``,
# ``coordination`` — plus *summoned units as references-by-version* (``DefinitionRef``),
# **never** the architecture (team topology, IO schema, dependencies).
#
# Load-bearing rules:
#   * JSON only (the DSPy stance: loading a state NEVER executes code — it carries knob
#     VALUES, no callables, no nested executable Definition).
#   * Only STATIC knobs move. A fluid value can never cross via a state dict (the knobs
#     enumerated below are all author/static config); a consequential knob (model /
#     policies) transferred is still static config, never a fluid-derived Sink target.
#   * Copy-on-write: ``load_state`` re-freezes via the Tuner's ``_refreeze``, minting a
#     fresh content sha — it never mutates the target Definition in place.
#   * References-by-version: a summoned unit is stored as ``{id, version}``, not the
#     embedded nested Definition — keeping the hash bounded and replay reproducible
#     (vision §5 open Q). Embedding a full nested Definition is rejected at validation.


class IncompatibleStateError(TypeError):
    """``load_state(strict=True)`` was asked to load a state onto an incompatible shape.

    Architecture (team topology / IO schema / dependencies) is identified by
    :attr:`StateDict.structure_sha`; a mismatch means the knobs would land on a different
    architecture. ``strict=True`` raises this; ``strict=False`` loads the structural
    intersection instead (only the roles/knobs both shapes share).
    """


class RoleKnobs(BaseModel):
    """The tunable knobs for one role — the per-role 'weights' (CRA-210).

    Every field is a STATIC, author-supplied knob the Tuner is allowed to search; none is
    fluid/session-derived. Decode knobs are carried only when pinned (``None`` ⇒ absent),
    mirroring the hash-neutral-when-None law on :class:`AgentSpec`.
    """

    model_config = {"frozen": True}

    prompt: str = ""
    model: str | list[str] | None = None
    context_strategy: str | None = None
    policies: list[str] = Field(default_factory=list)
    temperature: float | None = None
    top_p: float | None = None
    sample_k: int | None = None


class StateDict(BaseModel):
    """The tunable knobs of a Definition as references-by-version — the 'weights' (CRA-210).

    Carries ONLY what the Tuner/LearningLoop may move: per-role knobs (:class:`RoleKnobs`),
    the team ``coordination`` topology choice, ``injected_prompts``, and summoned units as
    ``DefinitionRef`` (``{id, version}``) references-by-version. It carries **no**
    architecture (team topology beyond the coordination choice, IO schema, dependency
    structure) and **no** executable nested Definition — JSON only.

    :attr:`structure_sha` is the content hash of the architecture the knobs were extracted
    from (sorted role set, IO parameter names/types/flows, dependency ids, coordination
    kind). Two Definitions with the same ``structure_sha`` are transfer-compatible.
    :attr:`sha` is the content hash of the knob VALUES — editing any knob changes it
    (the AC: "editing a knob changes ``StateDict.sha``").
    """

    model_config = {"frozen": True}

    roles: dict[str, RoleKnobs] = Field(default_factory=dict)
    coordination: Coordination = Coordination.SINGLE
    injected_prompts: list[Prompt] = Field(default_factory=list)
    # Summoned units as references-by-version — NEVER an embedded nested Definition.
    summons: list[DefinitionRef] = Field(default_factory=list)
    structure_sha: str = ""

    @property
    def sha(self) -> str:
        """Deterministic 12-char content hash of the knob VALUES (the 'weights' identity).

        Excludes :attr:`structure_sha` (that is architecture identity, not weights) so two
        states with identical knobs over different shapes still compare equal on ``sha``;
        editing any transferred knob value diverges it.
        """
        payload = {
            "roles": {r: k.model_dump(mode="json") for r, k in sorted(self.roles.items())},
            "coordination": self.coordination.value,
            "injected_prompts": [p.model_dump(mode="json") for p in self.injected_prompts],
            "summons": [s.model_dump(mode="json") for s in self.summons],
        }
        blob = json.dumps(payload, sort_keys=True, default=str).encode()
        return hashlib.sha256(blob).hexdigest()[:12]


def _structure_sha(definition: Definition) -> str:
    """Content hash of a Definition's ARCHITECTURE — the transfer-compatibility key.

    Folds the *shape*, never the tunable knobs: the sorted role set, the typed IO schema
    (each parameter's name/type/flow), the dependency ids, and the coordination *kind*.
    Two Definitions transfer-compatible iff this matches; editing a knob (prompt, model…)
    leaves it unchanged, so a knob edit never makes a state look incompatible.
    """
    payload = {
        "roles": sorted(a.role for a in definition.team.agents),
        "inputs": sorted((p.name, str(p.type), p.flow.value) for p in definition.inputs),
        "outputs": sorted((p.name, str(p.type), p.flow.value) for p in definition.outputs),
        "dependencies": sorted(d.id for d in definition.dependencies),
        "coordination": definition.team.coordination.value,
    }
    blob = json.dumps(payload, sort_keys=True, default=str).encode()
    return hashlib.sha256(blob).hexdigest()[:12]


def state_dict(definition: Definition) -> StateDict:
    """Extract a Definition's tunable knobs as a references-by-version :class:`StateDict`.

    Excludes architecture keys (team topology, IO schema, dependencies) by construction —
    only the per-role tunable knobs, the coordination choice, ``injected_prompts``, and the
    dependency *references* (as summoned-unit ``DefinitionRef``\\ s) are carried. Deterministic
    and JSON-only. ``d.load_state(d.state_dict())`` re-mints the same content sha
    (sha-identity), since the same knobs re-freeze to the same hash.
    """
    roles = {
        agent.role: RoleKnobs(
            prompt=agent.prompt,
            model=agent.model,
            context_strategy=agent.context_strategy,
            policies=list(agent.policies),
            temperature=agent.temperature,
            top_p=agent.top_p,
            sample_k=agent.sample_k,
        )
        for agent in definition.team.agents
    }
    # Summoned units travel as pinned references-by-version, not embedded Definitions:
    # the dependency refs the Definition already declares (id + version).
    summons = [DefinitionRef(id=d.id, version=d.version) for d in definition.dependencies]
    return StateDict(
        roles=roles,
        coordination=definition.team.coordination,
        injected_prompts=[p.model_copy(deep=True) for p in definition.injected_prompts],
        summons=summons,
        structure_sha=_structure_sha(definition),
    )


def load_state(
    definition: Definition,
    state: StateDict,
    *,
    strict: bool = True,
    only: list[str] | None = None,
) -> Definition:
    """Transfer learned knob VALUES from ``state`` onto ``definition`` (copy-on-write).

    Returns a NEW, re-frozen Definition (fresh content sha via the Tuner's ``_refreeze``) —
    the target is never mutated in place. Only STATIC knobs move; no fluid value can cross.

    * ``strict=True`` (default): raise :class:`IncompatibleStateError` if the architectures
      differ (``state.structure_sha != _structure_sha(definition)``).
    * ``strict=False``: load the structural **intersection** — apply knobs only for the
      roles present in BOTH shapes, skipping the rest.
    * ``only``: restrict which knob groups transfer. Members of
      ``{"prompt", "model", "context_strategy", "policies", "decode", "fewshots",
      "coordination"}``; e.g. ``only=["fewshots"]`` transfers only the injected few-shot
      prompts. ``None`` transfers everything.
    """
    target_structure = _structure_sha(definition)
    if strict and state.structure_sha and state.structure_sha != target_structure:
        raise IncompatibleStateError(
            f"state structure_sha {state.structure_sha!r} does not match target "
            f"{target_structure!r}; pass strict=False to load the structural intersection"
        )

    groups = set(only) if only is not None else None

    def _wants(group: str) -> bool:
        return groups is None or group in groups

    # -- per-role knobs (intersection of roles when not strict) -------------
    new_agents: list[AgentSpec] = []
    for agent in definition.team.agents:
        knobs = state.roles.get(agent.role)
        if knobs is None:
            new_agents.append(agent.model_copy(deep=True))
            continue
        updates: dict[str, object] = {}
        if _wants("prompt"):
            updates["prompt"] = knobs.prompt
        if _wants("model"):
            updates["model"] = knobs.model
        if _wants("context_strategy"):
            updates["context_strategy"] = knobs.context_strategy
        if _wants("policies"):
            updates["policies"] = list(knobs.policies)
        if _wants("decode"):
            for name in DECODE_KNOB_FIELDS:
                updates[name] = getattr(knobs, name)
        new_agents.append(agent.model_copy(update=updates, deep=True))

    new_team = definition.team.model_copy(
        update={
            "agents": new_agents,
            **({"coordination": state.coordination} if _wants("coordination") else {}),
        },
        deep=True,
    )

    def_updates: dict[str, object] = {"team": new_team}
    if _wants("fewshots"):
        def_updates["injected_prompts"] = [p.model_copy(deep=True) for p in state.injected_prompts]

    loaded = definition.model_copy(update=def_updates, deep=True)
    # Copy-on-write: re-freeze to mint a fresh content sha (never an in-place edit).
    return _refreeze(definition, loaded)


# ===========================================================================
# CRA-214 — AL-T6: the explore-rate dial (the serving-time bandit overlay)
# ---------------------------------------------------------------------------
# ``LearningLoop.improve`` is a one-shot OFFLINE optimizer; nothing routes a bounded
# fraction of LIVE items to a trial candidate and feeds the outcomes back. The
# :class:`ServingLoop` is that overlay: route ``(1-ε)`` of items to the promoted best and
# ``ε`` to a trial candidate, choosing which items explore by a **seeded hash of the
# recorded** ``item_id`` — so a replay re-explores EXACTLY the same items (deterministic
# under replay ⇒ identical graduation).
#
# Fixes applied (ML blockers from the issue):
#   * Bare fixed-ε is the weakest bandit ⇒ a **decaying-ε schedule** is the default, with a
#     typed :class:`ExploreStrategy` hook reserving UCB1/Thompson (they need only per-arm
#     reward mean + count, already in the emission ledger; the deterministic-hash router is
#     what ships).
#   * A continuously-re-tested gate has an optional-stopping/peeking failure mode ⇒
#     graduation uses a **pre-registered per-trial sample size** (no verdict before N
#     outcomes) — :meth:`ServingLoop.graduate` returns ``decided=False`` until N is reached,
#     controlling Type-I error under continuous peeking.
#   * ε is bounded by the shared ``CostBudget``: once exhausted, exploration stops (route
#     everything to the promoted best).
#
# Determinism: every stochastic choice derives from one recorded ``seed`` + the recorded
# ``item_id`` ⇒ identical explored subset ⇒ identical graduation. Only STATIC knobs are ever
# promoted (the trial graduates through the same eval gate as ``improve``).


class ExploreStrategy(str, Enum):
    """How a :class:`ServingLoop` chooses *which* items explore.

    ``HASH`` (the shipped, deterministic-under-replay router) routes by a seeded hash of the
    recorded ``item_id``. ``UCB1``/``THOMPSON`` are reserved hooks: they need only per-arm
    reward mean + count (already in the emission ledger) and are out-of-scope here as a
    *router*, declared so a future strategy plugs in without an API change.
    """

    HASH = "hash"
    UCB1 = "ucb1"
    THOMPSON = "thompson"


class ExploreSchedule(BaseModel):
    """The ε dial + its decay — a decaying-ε schedule (CRA-214).

    ``epsilon`` is the base explore rate in ``[0, 1]``; ``decay`` shrinks it as served items
    accumulate: the effective rate after ``n`` served items is
    ``epsilon / (1 + decay * n)`` (so ``decay=0`` is a flat fixed-ε). ``epsilon=0`` disables
    exploration entirely (the no-op overlay AC).
    """

    model_config = {"frozen": True}

    epsilon: float = 0.0
    decay: float = 0.0
    strategy: ExploreStrategy = ExploreStrategy.HASH

    def rate_at(self, served: int) -> float:
        """The effective explore rate after ``served`` items (decaying-ε)."""
        if self.epsilon <= 0.0:
            return 0.0
        return self.epsilon / (1.0 + self.decay * max(0, served))


def _explore_hash(item_id: str, seed: int) -> float:
    """A deterministic value in ``[0, 1)`` from ``(item_id, seed)`` — the explore die.

    Seeded SHA-256 over the recorded ``item_id`` (a trusted, static signal — never a fluid
    value): the same ``(item_id, seed)`` always yields the same fraction, so a replay
    re-explores exactly the same items. Mirrors the cassette-key hashing law in
    ``runtime/replay.py`` (sorted-JSON → sha256), keeping one hashing idiom in the codebase.
    """
    blob = json.dumps({"item_id": item_id, "seed": seed}, sort_keys=True).encode()
    digest = hashlib.sha256(blob).digest()
    # First 8 bytes → an integer in [0, 2**64), normalised to [0, 1).
    return int.from_bytes(digest[:8], "big") / float(1 << 64)


class ServingDecision(BaseModel):
    """The routing verdict for one live item (the audit record).

    ``explore`` is True iff the item was routed to the trial candidate. ``version`` is the
    routed Definition's ``str(version)``. The decision is a pure function of
    ``(item_id, seed, schedule, served, budget)`` — deterministic under replay.
    """

    model_config = {"frozen": True}

    item_id: str
    explore: bool
    version: str


class GraduationVerdict(BaseModel):
    """The pre-registered-N graduation decision for a trial arm (no-peeking, CRA-214).

    ``decided`` is False until ``n_outcomes >= sample_size`` — the gate refuses a verdict
    before the pre-registered sample size is reached, so continuous peeking cannot inflate
    the false-promotion rate. Once decided, ``graduate`` is True iff the trial's mean reward
    strictly beats the baseline's by at least ``min_lift`` (the eval gate still applies on
    promotion via the :class:`LearningLoop`).
    """

    model_config = {"frozen": True}

    decided: bool
    graduate: bool
    n_outcomes: int
    sample_size: int
    trial_mean: float
    baseline_mean: float
    reason: str


class ServingLoop:
    """A serving-time explore/exploit overlay over a promoted best + a trial candidate.

    Routes ``(1-ε)`` of live items to ``promoted`` and ``ε`` to ``trial``, choosing the
    explored items by a seeded hash of each recorded ``item_id`` (deterministic under
    replay). ε follows a decaying schedule and is bounded by the shared ``CostBudget``: once
    the budget is exhausted, every item routes to the promoted best (no exploration).

    The trial graduates ONLY through the eval gate — this loop decides *whether enough
    evidence has accrued* (pre-registered N), not whether to promote; promotion stays with
    the :class:`LearningLoop` (eval-gated + reversible). Both arms are frozen, eval-mode
    Definitions; only STATIC knobs are ever promoted.
    """

    def __init__(
        self,
        promoted: Definition,
        trial: Definition,
        schedule: ExploreSchedule,
        *,
        seed: int = 0,
        sample_size: int = 100,
        min_lift: float = 0.0,
        org_id: str = "local",
    ) -> None:
        if sample_size < 1:
            raise ValueError("sample_size (pre-registered N) must be >= 1")
        self.promoted = promoted
        self.trial = trial
        self.schedule = schedule
        self.seed = seed
        self.sample_size = sample_size
        self.min_lift = min_lift
        self.org_id = org_id
        self._served = 0

    def route(self, item_id: str, ctx: RunContext) -> ServingDecision:
        """Route one live item to the promoted best or the trial candidate.

        Explore iff the budget has room AND the seeded item hash falls under the effective
        (decaying) explore rate. Deterministic: same ``(item_id, seed, served, schedule)`` ⇒
        same decision. Advances the internal served counter (drives ε decay) — so a replay
        must route items in the same recorded order to reproduce exactly.
        """
        rate = self.schedule.rate_at(self._served)
        self._served += 1
        budget_ok = ctx.cost_budget.remaining_usd is None or ctx.cost_budget.remaining_usd > 0.0
        explore = rate > 0.0 and budget_ok and _explore_hash(item_id, self.seed) < rate
        chosen = self.trial if explore else self.promoted
        return ServingDecision(
            item_id=item_id,
            explore=explore,
            version=str(chosen.version),
        )

    def explored_items(self, item_ids: list[str], ctx: RunContext) -> list[str]:
        """The deterministic subset of ``item_ids`` routed to the trial (the explored set).

        A pure projection of :meth:`route` for the whole batch — same ``(seed, item_ids)`` ⇒
        identical explored subset (the AC). Resets and restores the served counter so it is a
        side-effect-free query.
        """
        saved = self._served
        try:
            self._served = 0
            return [i for i in item_ids if self.route(i, ctx).explore]
        finally:
            self._served = saved

    def graduate(
        self, trial_rewards: list[float], baseline_rewards: list[float]
    ) -> GraduationVerdict:
        """Decide whether the trial has accrued enough evidence to graduate (no-peeking).

        Returns ``decided=False`` until ``len(trial_rewards) >= sample_size`` — the
        pre-registered-N rule that controls Type-I error under continuous peeking. Once
        decided, graduates iff the trial's mean reward beats the baseline's by at least
        ``min_lift``. A trial that loses to baseline never graduates (so the promoted best is
        unchanged); the eval gate on the :class:`LearningLoop` still applies on promotion.
        """
        n = len(trial_rewards)
        trial_mean = sum(trial_rewards) / n if n else 0.0
        base_mean = sum(baseline_rewards) / len(baseline_rewards) if baseline_rewards else 0.0
        if n < self.sample_size:
            return GraduationVerdict(
                decided=False,
                graduate=False,
                n_outcomes=n,
                sample_size=self.sample_size,
                trial_mean=trial_mean,
                baseline_mean=base_mean,
                reason=f"peeking: {n}/{self.sample_size} outcomes — no verdict before N",
            )
        graduate = trial_mean - base_mean >= self.min_lift and trial_mean > base_mean
        reason = (
            f"trial mean {trial_mean:.4g} beats baseline {base_mean:.4g} by >= {self.min_lift:.4g}"
            if graduate
            else f"trial mean {trial_mean:.4g} does not beat baseline {base_mean:.4g}"
        )
        return GraduationVerdict(
            decided=True,
            graduate=graduate,
            n_outcomes=n,
            sample_size=self.sample_size,
            trial_mean=trial_mean,
            baseline_mean=base_mean,
            reason=reason,
        )
