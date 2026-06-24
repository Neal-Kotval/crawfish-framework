"""CRA-214 — AL-T6: the explore-rate dial (ServingLoop).

A serving-time explore/exploit overlay. These tests pin the load-bearing guarantees:

* ``epsilon=0`` ⇒ no exploration (a no-op overlay — every item routes to the promoted best);
* same ``(seed, item_ids)`` ⇒ identical explored subset (deterministic under replay);
* ε stops when the shared ``CostBudget`` is exhausted;
* graduation uses a pre-registered N (no verdict before N outcomes) — controls Type-I error
  under continuous peeking;
* a trial losing to baseline never graduates (the promoted best is unchanged);
* a decaying-ε schedule shrinks the rate as items accumulate.
"""

from __future__ import annotations

from crawfish.core.context import CostBudget, RunContext
from crawfish.core.types import Flow, Parameter
from crawfish.definition.types import AgentSpec, Definition, TeamSpec
from crawfish.learning import ExploreSchedule, ExploreStrategy, ServingLoop
from crawfish.store import SqliteStore
from crawfish.tuner import eval as eval_mode


def _defn(model: str) -> Definition:
    return eval_mode(
        Definition(
            team=TeamSpec(agents=[AgentSpec(role="worker", prompt="do it", model=model)]),
            inputs=[Parameter(name="task", type="text", flow=Flow.FLUID)],
        )
    )


def _ctx(tmp_path, *, limit_usd=None, spent=0.0) -> RunContext:
    store = SqliteStore(str(tmp_path / "t.db"))
    budget = CostBudget(limit_usd=limit_usd, spent_usd=spent)
    return RunContext(store=store, cost_budget=budget)


def _loop(schedule: ExploreSchedule, *, seed=0, sample_size=100, min_lift=0.0) -> ServingLoop:
    return ServingLoop(
        _defn("slow"),
        _defn("fast"),
        schedule,
        seed=seed,
        sample_size=sample_size,
        min_lift=min_lift,
    )


_ITEMS = [f"item-{i}" for i in range(200)]


# -- epsilon=0 ⇒ no exploration (no-op overlay) ------------------------------
def test_zero_epsilon_no_exploration(tmp_path) -> None:
    loop = _loop(ExploreSchedule(epsilon=0.0))
    ctx = _ctx(tmp_path)
    decisions = [loop.route(i, ctx) for i in _ITEMS]
    assert all(not d.explore for d in decisions)
    assert {d.version for d in decisions} == {str(loop.promoted.version)}


# -- same (seed, item_ids) ⇒ identical explored subset -----------------------
def test_same_seed_same_explored_subset(tmp_path) -> None:
    sched = ExploreSchedule(epsilon=0.3)
    a = _loop(sched, seed=42)
    b = _loop(sched, seed=42)
    ctx = _ctx(tmp_path)
    assert a.explored_items(_ITEMS, ctx) == b.explored_items(_ITEMS, ctx)
    # a non-empty subset actually explores (ε=0.3 over 200 items)
    assert 0 < len(a.explored_items(_ITEMS, ctx)) < len(_ITEMS)


# -- a different seed re-rolls a different (but still deterministic) subset ---
def test_different_seed_different_subset(tmp_path) -> None:
    sched = ExploreSchedule(epsilon=0.3)
    ctx = _ctx(tmp_path)
    s1 = _loop(sched, seed=1).explored_items(_ITEMS, ctx)
    s2 = _loop(sched, seed=2).explored_items(_ITEMS, ctx)
    assert s1 != s2


# -- explored_items is side-effect-free (does not advance the served counter) --
def test_explored_items_is_pure_query(tmp_path) -> None:
    loop = _loop(ExploreSchedule(epsilon=0.3), seed=7)
    ctx = _ctx(tmp_path)
    first = loop.explored_items(_ITEMS, ctx)
    second = loop.explored_items(_ITEMS, ctx)
    assert first == second


# -- route is deterministic per item given the same served position ----------
def test_route_deterministic(tmp_path) -> None:
    ctx = _ctx(tmp_path)
    a = _loop(ExploreSchedule(epsilon=0.5), seed=3)
    b = _loop(ExploreSchedule(epsilon=0.5), seed=3)
    da = [a.route(i, ctx).explore for i in _ITEMS]
    db = [b.route(i, ctx).explore for i in _ITEMS]
    assert da == db


# -- ε stops when the budget is exhausted ------------------------------------
def test_exploration_stops_when_budget_exhausted(tmp_path) -> None:
    # budget fully spent ⇒ remaining_usd == 0 ⇒ no exploration regardless of ε
    loop = _loop(ExploreSchedule(epsilon=1.0))
    ctx = _ctx(tmp_path, limit_usd=1.0, spent=1.0)
    decisions = [loop.route(i, ctx) for i in _ITEMS]
    assert all(not d.explore for d in decisions)

    # with budget remaining, ε=1.0 explores everything
    loop2 = _loop(ExploreSchedule(epsilon=1.0))
    ctx2 = _ctx(tmp_path, limit_usd=10.0, spent=0.0)
    assert all(d.explore for d in (loop2.route(i, ctx2) for i in _ITEMS))


# -- decaying-ε shrinks the effective rate as items accumulate ----------------
def test_decaying_epsilon_schedule() -> None:
    sched = ExploreSchedule(epsilon=0.5, decay=1.0)
    assert sched.rate_at(0) == 0.5
    assert sched.rate_at(1) == 0.25
    assert sched.rate_at(9) == 0.05
    # decay=0 is flat fixed-ε
    flat = ExploreSchedule(epsilon=0.4, decay=0.0)
    assert flat.rate_at(0) == flat.rate_at(1000) == 0.4


# -- strategy hook defaults to the deterministic HASH router -----------------
def test_default_strategy_is_hash() -> None:
    assert ExploreSchedule(epsilon=0.1).strategy is ExploreStrategy.HASH


# -- graduation: no verdict before the pre-registered N (no peeking) ----------
def test_graduation_no_verdict_before_n() -> None:
    loop = _loop(ExploreSchedule(epsilon=0.2), sample_size=50)
    verdict = loop.graduate([1.0] * 10, [0.0] * 10)  # only 10 < 50 outcomes
    assert verdict.decided is False
    assert verdict.graduate is False
    assert "peeking" in verdict.reason
    assert verdict.n_outcomes == 10 and verdict.sample_size == 50


# -- graduation: a winning trial graduates once N is reached ------------------
def test_graduation_winner_graduates_at_n() -> None:
    loop = _loop(ExploreSchedule(epsilon=0.2), sample_size=20)
    verdict = loop.graduate([1.0] * 20, [0.0] * 20)
    assert verdict.decided is True and verdict.graduate is True
    assert verdict.trial_mean == 1.0 and verdict.baseline_mean == 0.0


# -- graduation: a trial losing to baseline never graduates ------------------
def test_graduation_loser_never_graduates() -> None:
    loop = _loop(ExploreSchedule(epsilon=0.2), sample_size=20)
    verdict = loop.graduate([0.0] * 20, [1.0] * 20)
    assert verdict.decided is True and verdict.graduate is False
    # a tie also does not graduate (strict improvement required)
    tie = loop.graduate([0.5] * 20, [0.5] * 20)
    assert tie.decided is True and tie.graduate is False


# -- graduation honours min_lift (a marginal win below the lift floor fails) --
def test_graduation_respects_min_lift() -> None:
    loop = _loop(ExploreSchedule(epsilon=0.2), sample_size=10, min_lift=0.2)
    marginal = loop.graduate([0.55] * 10, [0.5] * 10)  # +0.05 lift < 0.2 floor
    assert marginal.decided is True and marginal.graduate is False
    clear = loop.graduate([0.8] * 10, [0.5] * 10)  # +0.3 lift >= 0.2 floor
    assert clear.decided is True and clear.graduate is True
