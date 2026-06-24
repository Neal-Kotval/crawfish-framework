"""QuorumRuntime — typed self-consistency (sample-k, vote) over any inner runtime.

Self-consistency is the cheapest, best-attested variance reducer and the purest
expression of the thesis: **k stochastic leaves reduced by a deterministic vote**. This
runtime WRAPS any inner :class:`~crawfish.runtime.base.AgentRuntime`, samples the *same*
:class:`RunRequest` ``k`` times, and reduces the ``k`` recorded results to one consensus
winner by a typed, pure :class:`ConsensusFn`.

It is the re-run sibling of :class:`~crawfish.runtime.escalate.EscalatingRuntime`: each of
the ``k`` samples charges the shared budget and emits through ``inner`` exactly as a
normal call (the ``escalate.py`` pattern), so the cost ledger and the event ledger see
every leaf. The consensus reduction is **pure** over the recorded ``RunResult.text`` —
no model call, no I/O, no wall-clock.

Determinism. Each sample is an isolated leaf stamped with a distinct, derived
``decode_seed`` (so a seed-honouring backend produces independent draws replayably) and,
under a :class:`~crawfish.runtime.replay.RecordReplayRuntime`, a distinct F-1
:class:`~crawfish.runtime.replay.ExecutionCoordinate` (``sample_index``) so the ``k``
recorded samples land in ``k`` distinct cassettes instead of colliding into one
(unanimous no-op, variance 0 — the cassette-collision SECURITY blocker). Same base seed
⇒ identical sample count + winner.

Bounds. The run is bounded by ``k`` + the cost budget + the cancel token, **never
wall-clock**. Every sample preflights ``remaining_usd`` and charges through ``inner``; a
budget below ``k`` × per-call stops early on whatever samples it could afford and votes
over those (never exceeding the ceiling). With ``early_stop`` the run may also stop once a
*sequential* proportion test (a Wilson lower bound on the leader's lead) shows the lead is
statistically real — F-8 optional-stopping, not a fixed-``0.8`` threshold.

Security. A fluid-derived sample output is **data**, never instructions; the vote tally
and the declared default are **static/trusted** and never fluid-derived. Taint is the
**union** across the ``k`` samples — a vote does not launder taint (ALG-7): the consensus
winner :class:`~crawfish.output.Output` is tainted iff *any* sample was tainted. Ties /
no-majority resolve to a **declared** default (Router parity), never a silent pick.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
from abc import ABC, abstractmethod

from crawfish.core.context import RunContext
from crawfish.core.types import JSONValue, Parameter
from crawfish.output import Output
from crawfish.runtime.base import AgentRuntime, RunRequest, RunResult
from crawfish.runtime.prompt import split_inputs
from crawfish.validation import canonicalize

__all__ = [
    "Sample",
    "ConsensusResult",
    "QuorumResult",
    "ConsensusFn",
    "MajorityVote",
    "majority_vote",
    "QuorumRuntime",
    "QuorumAbstention",
    "quorum_output",
]


# A high-cardinality output set where every candidate is unique has no meaningful
# plurality; rather than crown an arbitrary singleton we abstain (TS-4). This is the
# default ceiling on the *number of distinct candidates* relative to k below which a
# plurality is considered well-defined.
_DEFAULT_MAX_CARDINALITY_RATIO = 1.0


class QuorumAbstention(RuntimeError):
    """The vote was ill-defined (no plurality / high-cardinality) — abstain (TS-4).

    Raised by :meth:`QuorumRuntime.run` only when the consensus function abstains AND no
    declared ``default`` text is configured to stand in. With a configured default, the
    runtime resolves to the default winner instead of raising (Router dead-letter parity).
    """


@dataclasses.dataclass(frozen=True)
class Sample:
    """One stochastic leaf in the quorum: its recorded result and derived taint.

    ``tainted`` is computed from the request's *fluid* inputs (matching
    ``team._result_output``), so the consensus winner can union taint across samples
    without re-deriving it. ``key`` is the consensus function's canonicalised vote key for
    this sample (see :class:`MajorityVote`) — the sampler keys each sample through the same
    function the vote uses, so the running-leader early-stop and the final tally agree.
    """

    index: int
    result: RunResult
    tainted: bool
    key: str


@dataclasses.dataclass(frozen=True)
class ConsensusResult:
    """The pure outcome of a vote over a list of :class:`Sample`.

    ``winner_text`` is the elected representative result text (``None`` on abstention);
    ``abstained`` is True when the plurality is ill-defined. ``tally`` is the per-key vote
    count, and ``runner_up_gap`` is the lead of the winner over the second place (votes),
    surfaced for winner's-curse reporting on rubric-argmax consensus.
    """

    winner_text: str | None
    abstained: bool
    tally: dict[str, int]
    winner_key: str | None = None
    runner_up_gap: int = 0


class ConsensusFn(ABC):
    """A pure reduction of the recorded samples to one consensus outcome.

    Two responsibilities, kept on one object so they cannot drift: :meth:`key_of` maps a
    recorded :class:`RunResult` to the canonical *vote key* (its equivalence class), and
    :meth:`consensus` tallies a list of :class:`Sample` (already keyed via :meth:`key_of`)
    into a :class:`ConsensusResult`. Both are PURE — deterministic over their inputs, no
    model call, no I/O. The sampler keys every sample through :meth:`key_of`, so the
    running-leader early-stop and the final vote share one notion of equality.
    """

    @abstractmethod
    def key_of(self, result: RunResult) -> str:
        """The canonical vote key (equivalence class) for ``result``."""

    @abstractmethod
    def consensus(self, samples: list[Sample]) -> ConsensusResult:
        """Tally the (ordered, pre-keyed) ``samples`` into one outcome."""


@dataclasses.dataclass(frozen=True)
class QuorumResult:
    """The full quorum outcome: the winner ``RunResult``, its aggregate taint, and tally.

    The :class:`~crawfish.runtime.base.AgentRuntime` contract returns only the
    ``RunResult``, but ``RunResult`` has no taint field (taint lives on
    :class:`~crawfish.output.Output`). :meth:`QuorumRuntime.run_quorum` returns this richer
    shape so a caller can wrap the winner into a correctly-tainted Output via
    :func:`quorum_output` without re-deriving taint; :meth:`QuorumRuntime.run` projects it
    down to ``result`` for the plain seam.
    """

    result: RunResult
    tainted: bool
    consensus: ConsensusResult
    samples: list[Sample]


class MajorityVote(ConsensusFn):
    """Modal-output consensus: the most-frequent canonicalised candidate wins.

    The estimand is the **modal output** — ``argmax`` of the empirical vote distribution
    over canonicalised keys (sorted-key JSON of the value, or of ``field`` when given), so
    semantically-equal outputs are collapsed before tallying. Mandatory canonicalization
    means ``{"a":1,"b":2}`` and ``{"b":2,"a":1}`` map to one candidate. Ties break
    deterministically toward the **first-seen** key (sample order is preserved); the
    caller's declared ``default`` (in :class:`QuorumRuntime`) stands in on abstention.

    Ill-defined plurality ⇒ **abstain** (TS-4): when the candidates are too spread out
    (more distinct candidates than ``floor(k * max_cardinality_ratio)``, or every sample
    distinct when ``k > 1``), no plurality exists, so it abstains rather than crown an
    arbitrary singleton.
    """

    def __init__(
        self,
        *,
        field: str | None = None,
        max_cardinality_ratio: float = _DEFAULT_MAX_CARDINALITY_RATIO,
    ) -> None:
        self.field = field
        self.max_cardinality_ratio = max_cardinality_ratio

    def key_of(self, result: RunResult) -> str:
        """Canonicalise the result text (or its ``field``) to a stable vote key."""
        value: JSONValue = result.text
        decoded = _decode_json(result.text)
        if decoded is not None:
            value = decoded
        resolved = _field(value, self.field)
        return json.dumps(canonicalize(resolved), sort_keys=True, separators=(",", ":"))

    def consensus(self, samples: list[Sample]) -> ConsensusResult:
        if not samples:
            return ConsensusResult(winner_text=None, abstained=True, tally={})

        tally: dict[str, int] = {}
        first_text: dict[str, str] = {}
        for sample in samples:
            tally[sample.key] = tally.get(sample.key, 0) + 1
            first_text.setdefault(sample.key, sample.result.text)

        # High-cardinality guard: a plurality is ill-defined when the candidates are too
        # spread out (default: every sample distinct). Abstain (TS-4).
        distinct = len(tally)
        if distinct > max(1, math.floor(len(samples) * self.max_cardinality_ratio)):
            return ConsensusResult(winner_text=None, abstained=True, tally=dict(tally))
        if distinct == len(samples) and len(samples) > 1:
            return ConsensusResult(winner_text=None, abstained=True, tally=dict(tally))

        # argmax over the tally; first-seen order is the deterministic tie-break (dict
        # preserves insertion order, which is sample order).
        winner_key = max(tally, key=lambda k: tally[k])
        counts = sorted(tally.values(), reverse=True)
        gap = counts[0] - (counts[1] if len(counts) > 1 else 0)
        return ConsensusResult(
            winner_text=first_text[winner_key],
            abstained=False,
            tally=dict(tally),
            winner_key=winner_key,
            runner_up_gap=gap,
        )


def majority_vote(
    *,
    field: str | None = None,
    max_cardinality_ratio: float = _DEFAULT_MAX_CARDINALITY_RATIO,
) -> ConsensusFn:
    """Construct a :class:`MajorityVote` consensus (the modal-output estimand)."""
    return MajorityVote(field=field, max_cardinality_ratio=max_cardinality_ratio)


def _decode_json(text: str) -> JSONValue | None:
    """Decode ``text`` iff it is exactly one self-contained JSON document, else ``None``."""
    stripped = text.strip()
    if not stripped or stripped[0] not in "{[":
        return None
    try:
        decoded, end = json.JSONDecoder().raw_decode(stripped)
    except (ValueError, TypeError):
        return None
    if stripped[end:].strip():
        return None
    return decoded


def _field(value: JSONValue, field: str | None) -> JSONValue:
    """Resolve a dotted ``field`` within ``value`` (``None`` if any segment is absent)."""
    if field is None:
        return value
    cur: JSONValue = value
    for part in field.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur


def _derive_seed(base_seed: int, sample_index: int) -> int:
    """A distinct, deterministic per-sample decode seed.

    Folds ``(base_seed, sample_index)`` through SHA-256 so each of the ``k`` samples draws
    independently from a seed-honouring backend, yet the whole quorum is reproducible from
    ``base_seed`` alone. Truncated to a positive 63-bit int (provider-friendly).
    """
    blob = f"{base_seed}:{sample_index}".encode()
    digest = hashlib.sha256(blob).digest()
    return int.from_bytes(digest[:8], "big") & 0x7FFFFFFFFFFFFFFF


def _wilson_lower_bound(successes: int, n: int, *, z: float) -> float:
    """Wilson score interval lower bound for a proportion (pure, stdlib-only).

    Used by the sequential proportion test: the leader's share has a Wilson lower bound;
    once that bound exceeds 0.5 the lead is statistically real and the loop may stop
    (F-8 optional-stopping). At ``n == 0`` the bound is 0.0.
    """
    if n == 0:
        return 0.0
    phat = successes / n
    z2 = z * z
    denom = 1.0 + z2 / n
    centre = phat + z2 / (2 * n)
    margin = z * math.sqrt((phat * (1 - phat) + z2 / (4 * n)) / n)
    return (centre - margin) / denom


class QuorumRuntime(AgentRuntime):
    """Sample the same request ``k`` times and reduce by a typed, pure consensus vote.

    ``k`` defaults to the Definition's tunable ``sample_k`` knob (AL-T1, read via
    ``request.resolved_decode()``) so the Tuner can search the cheapest ``k`` that hits a
    reliability target; an explicit ``k`` overrides it, and ``3`` is the floor when neither
    is pinned. ``consensus`` is any :class:`ConsensusFn` (default :func:`majority_vote`).

    ``default_text`` is the **declared** fallback (Router dead-letter parity): on abstention
    or no-majority the runtime resolves to this result text instead of a silent pick. With
    no declared default, an abstention raises :class:`QuorumAbstention` — never a silent
    arbitrary winner.
    """

    name = "quorum"

    def __init__(
        self,
        inner: AgentRuntime,
        *,
        k: int | None = None,
        consensus: ConsensusFn | None = None,
        default_text: str | None = None,
        base_seed: int = 0,
        early_stop: bool = True,
        alpha: float = 0.05,
        min_k: int = 3,
    ) -> None:
        self._inner = inner
        self._k = k
        self._consensus = consensus or majority_vote()
        self._default_text = default_text
        self._base_seed = base_seed
        self._early_stop = early_stop
        self._alpha = alpha
        self._min_k = max(1, min_k)
        # Lifetime counters (handy for batch reporting): total leaf samples drawn and the
        # number of runs that stopped early (sequential test or budget).
        self.samples_drawn = 0
        self.early_stops = 0

    # -- the seam ------------------------------------------------------------
    async def run(self, request: RunRequest, ctx: RunContext) -> RunResult:
        """Run the quorum and project to the plain ``RunResult`` (aggregate taint dropped).

        Use :meth:`run_quorum` when you need the aggregate taint to wrap the winner into a
        correctly-tainted :class:`~crawfish.output.Output` (taint has no home on
        ``RunResult``).
        """
        return (await self.run_quorum(request, ctx)).result

    async def run_quorum(self, request: RunRequest, ctx: RunContext) -> QuorumResult:
        """Sample k times, vote, and return the winner + aggregate taint + tally."""
        samples = await self._collect_samples(request, ctx)
        if samples:
            consensus = self._consensus.consensus(samples)
        else:
            consensus = ConsensusResult(None, True, {})
        return self._resolve(consensus, samples)

    # -- sampling (the only impure part) -------------------------------------
    async def _collect_samples(self, request: RunRequest, ctx: RunContext) -> list[Sample]:
        from crawfish.runtime.replay import ExecutionCoordinate, RecordReplayRuntime

        k = self._resolve_k(request)
        _static, fluid = split_inputs(request.definition, request.inputs)
        sample_tainted = bool(fluid)
        is_replay = isinstance(self._inner, RecordReplayRuntime)

        samples: list[Sample] = []
        z = self._z_for_alpha()
        max_seen_cost = 0.0  # largest per-leaf cost observed — the budget preflight basis

        for index in range(k):
            ctx.cancel_token.raise_if_cancelled()
            if not self._can_afford(ctx, max_seen_cost):
                # Budget below the next per-call cost — stop early, vote over what we have,
                # never charging past the ceiling.
                self.early_stops += 1
                break

            seed = _derive_seed(self._base_seed, index)
            seeded = request.model_copy(update={"decode_seed": seed})
            if is_replay:
                # F-1: a distinct sample_index coordinate gives each sample its own
                # cassette, so k recorded samples never collide into one.
                coordinate = ExecutionCoordinate(sample_index=index)
                result = await self._inner.run(seeded, ctx, coordinate=coordinate)  # type: ignore[call-arg]
            else:
                result = await self._inner.run(seeded, ctx)
            ctx.cost_budget.charge(result.cost_usd)
            max_seen_cost = max(max_seen_cost, result.cost_usd)
            self.samples_drawn += 1

            # Key through the consensus fn, so the running-leader test and the final vote
            # agree on equivalence (e.g. both honour majority_vote(field="label")).
            key = self._consensus.key_of(result)
            samples.append(Sample(index=index, result=result, tainted=sample_tainted, key=key))

            # Sequential proportion test (F-8): track the running leader and stop once a
            # Wilson lower bound on its share exceeds 0.5 — the lead is statistically real.
            if self._early_stop and index + 1 >= self._min_k:
                _leader_key, successes = _running_leader(samples)
                lb = _wilson_lower_bound(successes, len(samples), z=z)
                if lb > 0.5:
                    self.early_stops += 1
                    break

        return samples

    # -- resolution (pure given the consensus + samples) ---------------------
    def _resolve(self, consensus: ConsensusResult, samples: list[Sample]) -> QuorumResult:
        if consensus.winner_text is None:
            if self._default_text is None:
                raise QuorumAbstention(
                    "quorum vote was ill-defined (no plurality / high-cardinality) and no "
                    "declared default was configured; declare a default_text to resolve"
                )
            winner_text = self._default_text
        else:
            winner_text = consensus.winner_text

        # Aggregate taint is the UNION across samples — a vote does not launder taint.
        aggregate_tainted = any(s.tainted for s in samples)
        # Carry the elected representative's session/model shape where possible; the
        # winning sample (if any) is the representative leaf. Cost is already charged
        # per-sample to the shared budget, so the returned RunResult reports the
        # representative leaf's cost (not the sum) — callers that re-charge from the result
        # never double-count the quorum.
        winner_sample = _winning_sample(consensus, samples)
        base = winner_sample.result if winner_sample is not None else RunResult()
        result = base.model_copy(update={"text": winner_text})
        return QuorumResult(
            result=result,
            tainted=aggregate_tainted,
            consensus=consensus,
            samples=list(samples),
        )

    # -- helpers -------------------------------------------------------------
    def _resolve_k(self, request: RunRequest) -> int:
        if self._k is not None:
            return max(1, self._k)
        knob = request.resolved_decode().get("sample_k")
        if isinstance(knob, int) and knob >= 1:
            return knob
        return self._min_k

    def _can_afford(self, ctx: RunContext, expected_cost: float) -> bool:
        """Preflight: can the budget afford the next leaf (at the largest seen cost)?

        With an unbounded budget every sample is affordable. With a ceiling, the next
        sample is refused once ``remaining`` would not cover another leaf at the largest
        per-leaf cost observed so far (``expected_cost``) — so the quorum stops cleanly
        *before* a charge that would breach, never exceeding the ceiling. The first sample
        (``expected_cost == 0``) always runs while any budget remains, matching the
        escalate-cascade contract.
        """
        remaining = ctx.cost_budget.remaining_usd
        if remaining is None:
            return True
        if expected_cost <= 0.0:
            return remaining > 0.0
        return remaining >= expected_cost

    def _z_for_alpha(self) -> float:
        """Two-sided normal quantile for the sequential test, derived from ``alpha``."""
        from crawfish.experiment import k_from_alpha

        return k_from_alpha(self._alpha)


def _running_leader(samples: list[Sample]) -> tuple[str, int]:
    """The current plurality leader key and its vote count over ``samples`` (pure)."""
    tally: dict[str, int] = {}
    for sample in samples:
        tally[sample.key] = tally.get(sample.key, 0) + 1
    leader = max(tally, key=lambda key: tally[key])
    return leader, tally[leader]


def _winning_sample(consensus: ConsensusResult, samples: list[Sample]) -> Sample | None:
    """The first sample whose key is the consensus winner (the representative leaf)."""
    if consensus.winner_key is None:
        return None
    for sample in samples:
        if sample.key == consensus.winner_key:
            return sample
    return None


def quorum_output(
    result: RunResult,
    *,
    produced_by: str,
    tainted: bool,
    output_schema: list[Parameter] | None = None,
) -> Output[JSONValue]:
    """Wrap a quorum :class:`RunResult` as a typed :class:`Output`, carrying aggregate taint.

    The consensus winner's :class:`Output` is tainted iff *any* sample was tainted (the
    union computed by :meth:`QuorumRuntime.run`) — a vote does not launder taint (ALG-7).
    """
    return Output(
        value=result.text,
        produced_by=produced_by,
        tainted=tainted,
        output_schema=list(output_schema or []),
    )
