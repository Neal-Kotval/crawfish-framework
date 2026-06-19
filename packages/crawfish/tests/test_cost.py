"""CRA-121 acceptance: cost preview, warn/stop budgets, and the live meter."""

from __future__ import annotations

import shutil
from datetime import UTC
from pathlib import Path

import pytest

from crawfish.cost import (
    DEFAULT_MODEL_PRICES,
    Budget,
    BudgetState,
    CostMeter,
    estimate_cost,
    spent_today,
)
from crawfish.definition import Definition
from crawfish.runtime.command import DEFAULT_MODEL
from crawfish.store.sqlite import SqliteStore

FIXTURES = Path(__file__).parent / "fixtures"


def _full(tmp_path: Path) -> Definition:
    target = tmp_path / "full"
    shutil.copytree(FIXTURES / "full", target, dirs_exist_ok=True)
    return Definition.from_package(str(target))


# -- estimate_cost ----------------------------------------------------------
def test_estimate_scales_with_items(tmp_path: Path) -> None:
    d = _full(tmp_path)
    one = estimate_cost(d, items=1)
    ten = estimate_cost(d, items=10)
    assert one.total_usd > 0.0
    assert ten.total_usd == pytest.approx(one.total_usd * 10)
    assert ten.per_item_usd == pytest.approx(one.per_item_usd)


def test_estimate_scales_with_team_size(tmp_path: Path) -> None:
    d = _full(tmp_path)
    est = estimate_cost(d, items=1)
    assert est.team_size == len(d.team.agents) == 3
    # per-item cost is the sum over agents of each resolved model's price.
    expected = sum(
        DEFAULT_MODEL_PRICES[a.model if isinstance(a.model, str) else DEFAULT_MODEL]
        for a in d.team.agents
    )
    assert est.per_item_usd == pytest.approx(expected)


def test_unpinned_agent_uses_default_model_price(tmp_path: Path) -> None:
    d = _full(tmp_path)
    # scout is unpinned -> priced at DEFAULT_MODEL; reviewer is pinned to opus.
    assert d.agent("scout").model is None
    est = estimate_cost(d, items=1)
    assert DEFAULT_MODEL in est.per_model
    assert est.per_model[DEFAULT_MODEL] > 0.0


def test_mock_model_is_free() -> None:
    from crawfish.definition.types import AgentSpec, TeamSpec
    from crawfish.definition.types import Definition as Def

    d = Def(team=TeamSpec(agents=[AgentSpec(role="m", model="mock")]))
    est = estimate_cost(d, items=5)
    assert est.total_usd == 0.0
    assert est.per_model == {"mock": 0.0}


def test_unknown_model_priced_as_free() -> None:
    from crawfish.definition.types import AgentSpec, TeamSpec
    from crawfish.definition.types import Definition as Def

    d = Def(team=TeamSpec(agents=[AgentSpec(role="x", model="who-knows")]))
    assert estimate_cost(d, items=3).total_usd == 0.0


def test_custom_price_table_overrides_defaults() -> None:
    from crawfish.definition.types import AgentSpec, TeamSpec
    from crawfish.definition.types import Definition as Def

    d = Def(team=TeamSpec(agents=[AgentSpec(role="a", model="x")]))
    est = estimate_cost(d, items=2, model_prices={"x": 1.5})
    assert est.per_item_usd == pytest.approx(1.5)
    assert est.total_usd == pytest.approx(3.0)


def test_negative_items_rejected() -> None:
    from crawfish.definition.types import Definition as Def

    with pytest.raises(ValueError):
        estimate_cost(Def(), items=-1)


# -- Budget.check -----------------------------------------------------------
def test_budget_check_thresholds() -> None:
    b = Budget(stop_usd=10.0, warn_usd=8.0)
    assert b.check(0.0) is BudgetState.OK
    assert b.check(7.99) is BudgetState.OK
    assert b.check(8.0) is BudgetState.WARN  # at warn
    assert b.check(9.5) is BudgetState.WARN
    assert b.check(10.0) is BudgetState.STOPPED  # at stop
    assert b.check(11.0) is BudgetState.STOPPED  # over stop


def test_budget_default_warn_is_eighty_percent() -> None:
    b = Budget(stop_usd=100.0)
    assert b.warn_usd == pytest.approx(80.0)
    assert b.check(80.0) is BudgetState.WARN


def test_unbounded_budget_is_always_ok() -> None:
    b = Budget()
    assert b.check(1_000_000.0) is BudgetState.OK


def test_warn_above_stop_rejected() -> None:
    with pytest.raises(ValueError):
        Budget(stop_usd=5.0, warn_usd=9.0)


def test_budget_projects_hard_cost_budget() -> None:
    cb = Budget(stop_usd=10.0).as_cost_budget(spent_usd=3.0)
    assert cb.limit_usd == 10.0
    assert cb.spent_usd == 3.0


# -- spent_today ------------------------------------------------------------
def test_spent_today_sums_event_costs() -> None:
    from datetime import datetime

    store = SqliteStore(":memory:")
    today = datetime.now(UTC)
    store.append_event(
        "run-1",
        {"kind": "runtime.run", "cost_usd": 0.25, "ts": today.isoformat()},
    )
    store.append_event(
        "run-1",
        {"kind": "run.finish", "cost_usd": 0.50, "ts": today.isoformat()},
    )
    store.append_event(
        "run-1",
        {"kind": "runtime.tool", "cost_usd": 99.0, "ts": today.isoformat()},  # ignored kind
    )
    total = spent_today(store, run_ids=["run-1"], now=today)
    assert total == pytest.approx(0.75)
    store.close()


def test_spent_today_excludes_other_days() -> None:
    from datetime import datetime, timedelta

    store = SqliteStore(":memory:")
    now = datetime.now(UTC)
    yesterday = now - timedelta(days=1)
    store.append_event("r", {"kind": "run.finish", "cost_usd": 1.0, "ts": now.isoformat()})
    store.append_event("r", {"kind": "run.finish", "cost_usd": 5.0, "ts": yesterday.isoformat()})
    assert spent_today(store, run_ids=["r"], now=now) == pytest.approx(1.0)
    store.close()


def test_spent_today_no_runs_is_zero() -> None:
    store = SqliteStore(":memory:")
    assert spent_today(store) == 0.0
    store.close()


# -- CostMeter --------------------------------------------------------------
def test_meter_accumulates_and_reports_remaining() -> None:
    meter = CostMeter(budget=Budget(stop_usd=10.0, warn_usd=8.0))
    assert meter.total_usd == 0.0
    assert meter.remaining_usd == pytest.approx(10.0)

    assert meter.charge(3.0) is BudgetState.OK
    assert meter.total_usd == pytest.approx(3.0)
    assert meter.remaining_usd == pytest.approx(7.0)

    assert meter.charge(5.0) is BudgetState.WARN  # 8.0 total
    assert meter.charge(2.0) is BudgetState.STOPPED  # 10.0 total
    assert meter.remaining_usd == pytest.approx(0.0)


def test_meter_remaining_clamps_at_zero() -> None:
    meter = CostMeter(budget=Budget(stop_usd=1.0))
    meter.charge(5.0)
    assert meter.remaining_usd == 0.0


def test_meter_unbounded_remaining_is_none() -> None:
    meter = CostMeter()
    meter.charge(42.0)
    assert meter.remaining_usd is None
    assert meter.state() is BudgetState.OK


def test_meter_rejects_negative_charge() -> None:
    with pytest.raises(ValueError):
        CostMeter().charge(-1.0)
