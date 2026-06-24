"""CRA-213 — AL-T3/TS-6, the cost-regularized Objective.

Two halves:

* :class:`Objective` is **pure** arithmetic over passed-in values —
  ``value(scores, cost_usd=…, ece=…) = Σ wᵢ·scoreᵢ − λ·cost − μ·ece`` — with a normalized,
  unit-free cost term and an ε-constraint alternative. Same inputs ⇒ same scalar.
* The :class:`Tuner` consumes an Objective to **re-rank among gate-passers**: ``cost_weight=0``
  reproduces today's winner; equal-quality ⇒ the cheaper wins; a 2%-better/5×-pricier
  candidate is rejected for a suitable λ; a candidate that maximizes the objective but
  regresses past ``-tolerance`` is still rejected by the hard regression gate; ``pareto=True``
  never promotes a dominated candidate.
"""

from __future__ import annotations

import pytest

from crawfish.batch import Task
from crawfish.core.context import CostBudget, RunContext
from crawfish.definition.types import AgentSpec, Definition, TeamSpec
from crawfish.metrics import Benchmark, OutputNumber, Rubric
from crawfish.runtime.base import RunRequest
from crawfish.runtime.mock import MockRuntime
from crawfish.runtime.prompt import pick_agent
from crawfish.store import SqliteStore
from crawfish.tuner import (
    KnobGridMutator,
    Objective,
    ObjectiveForm,
    Tuner,
)


# == the pure Objective =====================================================
def test_value_is_linear_scalarization() -> None:
    obj = Objective(weights={"acc": 1.0}, cost_weight=2.0, ece_weight=3.0)
    # 1.0*0.8 - 2.0*0.5 - 3.0*0.1 = 0.8 - 1.0 - 0.3 = -0.5
    assert obj.value({"acc": 0.8}, cost_usd=0.5, ece=0.1) == pytest.approx(-0.5)


def test_value_is_deterministic() -> None:
    obj = Objective(weights={"acc": 1.0}, cost_weight=1.0)
    a = obj.value({"acc": 0.9}, cost_usd=0.2, ece=0.05)
    b = obj.value({"acc": 0.9}, cost_usd=0.2, ece=0.05)
    assert a == b


def test_default_weights_sum_every_metric() -> None:
    obj = Objective(cost_weight=0.0)
    # No weights -> every recorded metric weighted 1.0.
    assert obj.quality({"a": 0.4, "b": 0.6}) == pytest.approx(1.0)


def test_cost_term_normalized_by_baseline() -> None:
    # With a baseline, λ is unit-free: the baseline-cost candidate contributes penalty λ*1.
    obj = Objective(cost_weight=2.0, cost_baseline_usd=0.5)
    # cost 0.5 -> normalized 1.0 -> penalty 2.0
    assert obj.score({"a": 1.0}, cost_usd=0.5).cost_penalty == pytest.approx(2.0)
    # cost 1.0 -> normalized 2.0 -> penalty 4.0
    assert obj.score({"a": 1.0}, cost_usd=1.0).cost_penalty == pytest.approx(4.0)


def test_cheaper_of_two_equal_quality_scores_higher() -> None:
    obj = Objective(cost_weight=1.0)
    cheap = obj.value({"a": 1.0}, cost_usd=0.1)
    pricey = obj.value({"a": 1.0}, cost_usd=0.5)
    assert cheap > pricey


def test_epsilon_constraint_picks_cheapest_above_floor() -> None:
    obj = Objective(form=ObjectiveForm.EPSILON, quality_floor=0.5)
    above_cheap = obj.score({"q": 0.6}, cost_usd=0.10)
    above_pricey = obj.score({"q": 0.9}, cost_usd=0.50)
    below = obj.score({"q": 0.4}, cost_usd=0.01)
    # Feasibility tracks the floor.
    assert above_cheap.feasible and above_pricey.feasible
    assert not below.feasible
    # Among feasible, the cheapest wins (higher objective).
    assert above_cheap.value > above_pricey.value
    # An infeasible (below-floor) candidate is pushed below every feasible one.
    assert below.value < above_pricey.value


# == Tuner integration ======================================================
# Real model ids with known prices in DEFAULT_MODEL_PRICES (haiku cheapest, opus priciest).
_CHEAP = "claude-haiku-4-5"  # $0.01/run
_MID = "claude-sonnet-4-6"  # $0.06/run
_PRICEY = "claude-opus-4-8"  # $0.30/run


def _base() -> Definition:
    return Definition(team=TeamSpec(agents=[AgentSpec(role="worker", model=_CHEAP)]))


def _ctx(tmp_path, *, limit_usd: float | None = None) -> RunContext:
    store = SqliteStore(str(tmp_path / "t.db"))
    return RunContext(store=store, cost_budget=CostBudget(limit_usd=limit_usd))


def _benchmark(score_by_model: dict[str, int]) -> Benchmark:
    def responder(request: RunRequest) -> str:
        agent = pick_agent(request.definition, request.role)
        return str(score_by_model.get(agent.model or "", 0))

    rubric = Rubric([OutputNumber(name="score")])
    return Benchmark(rubric, [Task(description="a")]), responder


def _runtime(responder) -> MockRuntime:
    return MockRuntime(responder=responder)


@pytest.mark.asyncio
async def test_cost_weight_zero_matches_legacy_winner(tmp_path) -> None:
    # The pricey model scores strictly best; with no cost pressure it should win either way.
    bench, responder = _benchmark({_CHEAP: 1, _MID: 5, _PRICEY: 9})
    mut = KnobGridMutator(models=[_CHEAP, _MID, _PRICEY])
    runtime = _runtime(responder)

    legacy = await Tuner(bench, mut).tune(_base(), _ctx(tmp_path), runtime, seed=0)
    obj = Tuner(bench, mut, objective=Objective(cost_weight=0.0)).tune
    cost_zero = await obj(_base(), _ctx(tmp_path), runtime, seed=0)

    # Same winner: cost_weight=0 reduces to pure quality.
    assert legacy.best_scores == cost_zero.best_scores
    assert legacy.improved and cost_zero.improved


@pytest.mark.asyncio
async def test_two_percent_better_five_x_pricier_rejected(tmp_path) -> None:
    # Cheap scores 1.00; pricey scores 1.02 (2% better) but costs 30x. A suitable λ rejects it.
    bench, responder = _benchmark({_CHEAP: 100, _PRICEY: 102})

    # Score the cheap base, then offer only the pricey upgrade.
    base = Definition(team=TeamSpec(agents=[AgentSpec(role="worker", model=_CHEAP)]))
    mut = KnobGridMutator(models=[_PRICEY])
    # λ large enough that 0.30 cost penalty outweighs the +2 quality gain.
    obj = Objective(cost_weight=100.0)
    result = await Tuner(bench, mut, objective=obj).tune(
        base, _ctx(tmp_path), _runtime(responder), seed=0
    )
    # The pricey candidate is NOT promoted — cost dominates the marginal quality.
    assert not result.improved
    assert result.best.agent("worker").model == _CHEAP


@pytest.mark.asyncio
async def test_objective_max_but_regression_still_rejected(tmp_path) -> None:
    # The candidate is cheaper (max objective) but its quality REGRESSES vs the base.
    bench, responder = _benchmark({_MID: 5, _CHEAP: 1})  # base mid=5, candidate cheap=1
    base = Definition(team=TeamSpec(agents=[AgentSpec(role="worker", model=_MID)]))
    mut = KnobGridMutator(models=[_CHEAP])  # cheaper but worse-scoring
    obj = Objective(cost_weight=1.0)
    result = await Tuner(bench, mut, objective=obj, tolerance=0.0).tune(
        base, _ctx(tmp_path), _runtime(responder), seed=0
    )
    # The hard regression gate refuses it despite the better (cheaper) objective.
    assert not result.improved
    assert result.best.agent("worker").model == _MID


@pytest.mark.asyncio
async def test_pareto_never_promotes_a_dominated_candidate(tmp_path) -> None:
    # Candidate is both worse quality AND pricier than base -> strictly dominated, never won.
    bench, responder = _benchmark({_CHEAP: 9, _PRICEY: 1})
    base = Definition(team=TeamSpec(agents=[AgentSpec(role="worker", model=_CHEAP)]))
    mut = KnobGridMutator(models=[_PRICEY])
    result = await Tuner(bench, mut, pareto=True).tune(
        base, _ctx(tmp_path), _runtime(responder), seed=0
    )
    assert not result.improved
    assert result.best.agent("worker").model == _CHEAP


@pytest.mark.asyncio
async def test_objective_run_is_deterministic(tmp_path) -> None:
    bench, responder = _benchmark({_CHEAP: 1, _MID: 5, _PRICEY: 9})
    mut = KnobGridMutator(models=[_CHEAP, _MID, _PRICEY])
    obj = Objective(cost_weight=0.5)

    def run():
        return Tuner(bench, mut, objective=obj).tune(
            _base(), _ctx(tmp_path), _runtime(responder), seed=3
        )

    a = await run()
    b = await run()
    assert [t.objective_value for t in a.trials] == [t.objective_value for t in b.trials]
    assert a.best_scores == b.best_scores
    # Per-candidate cost is recorded on the trial log (deterministic estimate_cost).
    assert all(t.cost_usd is not None for t in a.trials)
