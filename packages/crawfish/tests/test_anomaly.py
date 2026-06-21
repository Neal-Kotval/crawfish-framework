"""CRA-190 acceptance: anomaly rules + auto-halt over the emission stream.

Deterministic — every test builds a synthetic :class:`Emission` stream with explicit
``ts`` and asserts the tiered response (FLAG/ALERT/HALT), that a runaway cost/flood
trips the run's :class:`CancelToken` and forces :class:`BudgetExceeded`, and that a
HALT decision derived from *tainted* emissions is not spoofable. No live model calls,
no wall clock affecting outcomes.
"""

from __future__ import annotations

import pytest

from crawfish.anomaly import (
    AnomalyEngine,
    BudgetApproachingRule,
    CostSpikeRule,
    EmissionFloodRule,
    FailureRateRule,
    Response,
    StuckRunRule,
    read_and_guard,
)
from crawfish.core.context import BudgetExceeded, CostBudget, RunContext
from crawfish.emission import Emission, EmissionKind, emit
from crawfish.observe import ObserverSurface, Severity
from crawfish.store import SqliteStore

NOW = 1_000_000.0


def _model(run_id: str, cost: float, *, ts: float, tainted: bool = False) -> Emission:
    return Emission(
        kind=EmissionKind.MODEL,
        run_id=run_id,
        pipeline="p",
        ts=ts,
        attrs={"model": "mock", "cost_usd": cost},
        tainted=tainted,
    )


def _finish(run_id: str, status: str, *, ts: float) -> Emission:
    return Emission(
        kind=EmissionKind.RUN_FINISH,
        run_id=run_id,
        pipeline="p",
        ts=ts,
        attrs={"status": status},
    )


def _start(run_id: str, *, ts: float) -> Emission:
    return Emission(
        kind=EmissionKind.RUN_START,
        run_id=run_id,
        pipeline="p",
        ts=ts,
        attrs={"runtime": "mock"},
    )


def _ctx() -> RunContext:
    return RunContext(store=SqliteStore(), run_id="r1", cost_budget=CostBudget(limit_usd=10.0))


# -- tiered response: same breach flags -> alerts -> halts --------------------


def test_cost_spike_tiers_flag_alert_halt_deterministically() -> None:
    stream = [_model("r1", 3.0, ts=NOW)]
    for response, severity in [
        (Response.FLAG, Severity.WARN),
        (Response.ALERT, Severity.CRITICAL),
        (Response.HALT, Severity.CRITICAL),
    ]:
        engine = AnomalyEngine([CostSpikeRule(threshold_usd=2.0, window="-5m", response=response)])
        firings = engine.evaluate(stream, now=NOW)
        assert len(firings) == 1
        firing = firings[0]
        assert firing.response is response
        assert firing.event.severity is severity
        assert firing.event.kind == "cost.spike"
        assert firing.halts is (response is Response.HALT)


def test_below_threshold_does_not_fire() -> None:
    engine = AnomalyEngine([CostSpikeRule(threshold_usd=5.0, window="-5m")])
    assert engine.evaluate([_model("r1", 1.0, ts=NOW)], now=NOW) == []


# -- HALT trips CancelToken AND forces BudgetExceeded ------------------------


def test_halt_trips_cancel_token_and_budget() -> None:
    ctx = _ctx()
    ctx.cost_budget.charge(4.0)  # in-budget so far (limit 10)
    assert not ctx.cancel_token.cancelled
    engine = AnomalyEngine([CostSpikeRule(threshold_usd=2.0, window="-5m", response=Response.HALT)])
    firings = engine.guard(ctx, [_model("r1", 3.0, ts=NOW)], now=NOW)
    assert firings and firings[0].halts
    # cooperative kill-switch tripped
    assert ctx.cancel_token.cancelled
    with pytest.raises(Exception, match="cancelled"):
        ctx.cancel_token.raise_if_cancelled()
    # budget ceiling forced below spend: the next charge raises BudgetExceeded
    with pytest.raises(BudgetExceeded):
        ctx.cost_budget.charge(0.01)


def test_flag_does_not_halt() -> None:
    ctx = _ctx()
    engine = AnomalyEngine([CostSpikeRule(threshold_usd=2.0, window="-5m", response=Response.FLAG)])
    firings = engine.guard(ctx, [_model("r1", 3.0, ts=NOW)], now=NOW)
    assert firings and not firings[0].halts
    assert not ctx.cancel_token.cancelled
    ctx.cost_budget.charge(1.0)  # still spendable, no exception


def test_halt_on_unbounded_budget_still_trips() -> None:
    # an unbounded (limit_usd=None) budget must still become un-chargeable after HALT
    ctx = RunContext(store=SqliteStore(), run_id="r1", cost_budget=CostBudget())
    ctx.cost_budget.charge(2.5)
    engine = AnomalyEngine([CostSpikeRule(threshold_usd=2.0, window="-5m", response=Response.HALT)])
    engine.guard(ctx, [_model("r1", 3.0, ts=NOW)], now=NOW)
    assert ctx.cancel_token.cancelled
    with pytest.raises(BudgetExceeded):
        ctx.cost_budget.charge(0.0001)


def test_halt_at_zero_spend_blocks_even_a_zero_charge() -> None:
    # A non-cost HALT before any model spend (spent_usd == 0) must still make the
    # budget lever trip — even a charge(0.0) — so the kill-switch is unconditional.
    ctx = RunContext(store=SqliteStore(), run_id="r1", cost_budget=CostBudget())
    assert ctx.cost_budget.spent_usd == 0.0
    engine = AnomalyEngine([EmissionFloodRule(max_count=2, window="-1m")])
    flood = [_model("r1", 0.0, ts=NOW - i * 0.001) for i in range(5)]
    firings = engine.guard(ctx, flood, now=NOW)
    assert firings and firings[0].response is Response.HALT
    assert ctx.cancel_token.cancelled
    with pytest.raises(BudgetExceeded):
        ctx.cost_budget.charge(0.0)


def test_budget_approaching_rejects_zero_budget() -> None:
    with pytest.raises(ValueError, match="budget_usd must be > 0"):
        BudgetApproachingRule(budget_usd=0.0)


# -- runaway kill-switch: emission flood / loop ------------------------------


def test_emission_flood_kill_switch_halts_batch() -> None:
    ctx = _ctx()
    flood = [_model("r1", 0.001, ts=NOW - i * 0.001) for i in range(60)]
    engine = AnomalyEngine([EmissionFloodRule(max_count=50, window="-1m")])
    firings = engine.guard(ctx, flood, now=NOW)
    assert firings and firings[0].response is Response.HALT
    assert firings[0].event.kind == "emission.flood"
    assert ctx.cancel_token.cancelled


# -- failure-rate windowed ---------------------------------------------------


def test_failure_rate_alert() -> None:
    stream = [
        _finish("a", "failed", ts=NOW - 10),
        _finish("b", "failed", ts=NOW - 8),
        _finish("c", "done", ts=NOW - 6),
    ]
    engine = AnomalyEngine([FailureRateRule(threshold=0.5, window="-1h", response=Response.ALERT)])
    firings = engine.evaluate(stream, now=NOW)
    assert firings and firings[0].response is Response.ALERT
    assert firings[0].event.data["failed"] == 2


# -- stuck run by emission ts (no wall clock) --------------------------------


def test_stuck_run_uses_emission_ts() -> None:
    stream = [_start("a", ts=NOW - 120), _finish("b", "done", ts=NOW)]
    engine = AnomalyEngine([StuckRunRule(seconds=60.0, response=Response.HALT)])
    firings = engine.evaluate(stream, now=NOW)
    assert firings and firings[0].event.data["run_id"] == "a"
    # a finished run of the same age must NOT be flagged
    engine2 = AnomalyEngine([StuckRunRule(seconds=60.0)])
    closed = [_start("a", ts=NOW - 120), _finish("a", "done", ts=NOW)]
    assert engine2.evaluate(closed, now=NOW) == []


def test_budget_approaching_early_warning() -> None:
    stream = [_model("r1", 8.5, ts=NOW)]
    engine = AnomalyEngine([BudgetApproachingRule(budget_usd=10.0, fraction=0.8)])
    firings = engine.evaluate(stream, now=NOW)
    assert firings and firings[0].response is Response.ALERT


# -- security: a HALT derived from tainted emissions is not spoofable --------


def test_tainted_emissions_cannot_bypass_halt() -> None:
    ctx = _ctx()
    # untrusted (tainted) content carries a real cost spike: the rule acts on the
    # typed numeric signal, not the free text, so the halt still fires and is recorded
    # as tainted-provenance without weakening the decision.
    tainted_stream = [_model("r1", 9.0, ts=NOW, tainted=True)]
    engine = AnomalyEngine([CostSpikeRule(threshold_usd=2.0, window="-5m", response=Response.HALT)])
    firings = engine.guard(ctx, tainted_stream, now=NOW)
    assert firings and firings[0].halts
    assert firings[0].tainted is True
    assert ctx.cancel_token.cancelled


# -- guard persists findings onto the observer surface (dashboard sees them) --


def test_guard_emits_findings_to_surface() -> None:
    ctx = _ctx()
    engine = AnomalyEngine(
        [CostSpikeRule(threshold_usd=2.0, window="-5m", response=Response.ALERT)]
    )
    engine.guard(ctx, [_model("r1", 3.0, ts=NOW)], now=NOW)
    events = ObserverSurface(ctx.store).events("p", kind="cost.spike")
    assert events and events[0].severity is Severity.CRITICAL
    assert events[0].run_id == "r1"


# -- read_and_guard: live-tail wiring from the store -------------------------


def test_read_and_guard_reads_stream_and_halts() -> None:
    ctx = _ctx()
    for i in range(5):
        emit(ctx.store, _model("r1", 1.0, ts=NOW - i))
    engine = AnomalyEngine([CostSpikeRule(threshold_usd=3.0, window="-5m", response=Response.HALT)])
    firings = read_and_guard(ctx, engine, run_id="r1", pipeline="p", now=NOW)
    assert firings and firings[0].halts
    assert ctx.cancel_token.cancelled


def test_enforce_budget_cancels_on_breach() -> None:
    ctx = RunContext(store=SqliteStore(), run_id="r1", cost_budget=CostBudget(limit_usd=1.0))
    AnomalyEngine.enforce_budget(ctx, 0.5)
    assert not ctx.cancel_token.cancelled
    with pytest.raises(BudgetExceeded):
        AnomalyEngine.enforce_budget(ctx, 1.0)
    assert ctx.cancel_token.cancelled
