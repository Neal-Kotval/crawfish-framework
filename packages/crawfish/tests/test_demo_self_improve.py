"""Deterministic, cassette/mock-backed acceptance test for the Milestone-F demo.

Runs the all-nine-F-features scenario (``demo/triage-bot/self_improve.py``) entirely
off the mock runtime — NO live model calls — and asserts the three load-bearing
guarantees the milestone promises:

* the **gate decision** fires (the cooler candidate temperature is promoted on a
  held-out gate set the tuner never saw);
* the **$0-resume** holds (re-running the bounded loop after it reached its fixed
  point skips every completed visit and charges zero new model calls);
* the **no-progress stop** holds (the loop halts when ``output_content_sha`` is
  unchanged across iterations).

Plus the supporting F-feature invariants (corpus poisoning quarantined, tune/gate
disjoint, worst-case cost within budget, cross-tenant isolation, frozen-only sink).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCENARIO = REPO_ROOT / "demo" / "triage-bot" / "self_improve.py"


def _load_scenario():
    spec = importlib.util.spec_from_file_location("crawfish_demo_self_improve_test", SCENARIO)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # so dataclass forward-refs resolve
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def result():
    if not SCENARIO.exists():  # pragma: no cover - demo always present in-repo
        pytest.skip(f"demo scenario not found at {SCENARIO}")
    module = _load_scenario()
    return module.run_self_improvement(live=False)  # deterministic mock path only


def test_scenario_passes_end_to_end(result) -> None:
    """The whole 10-step scenario passes its own composite predicate."""
    assert result.passed(), result.summary()


def test_all_ten_steps_recorded(result) -> None:
    """Every numbered step (0..10) produced evidence."""
    step_numbers = {s.n for s in result.steps}
    assert step_numbers >= set(range(0, 11)), step_numbers


# --- F-3 / F-8: the promotion gate fired -----------------------------------------
def test_gate_promoted(result) -> None:
    assert result.gate is not None
    assert result.gate.promoted is True, result.gate.reason
    # the cooler candidate beat the hotter baseline
    assert result.promoted_temperature < result.baseline_temperature


def test_winners_curse_shrink_does_not_inflate(result) -> None:
    """The de-biased (shrunk) score never exceeds the achievable max (1.0)."""
    assert 0.0 <= result.shrunk_score <= 1.0


# --- F-2 / F-0: $0-resume and no-progress stop -----------------------------------
def test_zero_dollar_resume(result) -> None:
    """Re-running the converged loop charges ZERO new model calls."""
    assert result.resume_extra_charges == 0


def test_no_progress_stop(result) -> None:
    """The loop halted at a fixed point — bounded, well under the 4-iter ceiling."""
    assert result.loop_fixed_point_sha
    assert 0 < result.loop_iterations_run < 4


# --- F-6: worst-case cost within budget AND an honest bound on actual spend ------
def test_worst_case_cost_within_budget(result) -> None:
    # Mock path is zero-cost, so worst-case is $0 (it is priced off the selected
    # model — mock = $0/call). The cost interval still must not exceed the budget.
    assert result.worst_case_usd <= result.budget_usd
    # And the worst-case must HONESTLY bound what was actually spent (F-6 integrity).
    assert result.total_spend_usd <= result.worst_case_usd


def test_cost_is_priced_off_selected_model() -> None:
    """The asserted worst-case is tied to the model's per-call price: a live (haiku)
    estimate is a positive, honest bound; the mock estimate is $0."""
    module = _load_scenario()
    mock = module.run_self_improvement(live=False)
    assert mock.worst_case_usd == 0.0  # mock = $0/call
    # The live per-call price table prices haiku above zero, so a live worst-case
    # would be a positive bound (we don't make a live call here — just the price).
    assert module._LIVE_PER_CALL_USD["claude-haiku-4-5"] > 0.0


# --- F-4 + security: corpus poisoning quarantined, cross-tenant isolation ---------
def test_trusted_corpus_only(result) -> None:
    """Org A's gold set is the trusted corrections only (the untrusted/tainted one
    was quarantined: 6 trusted seeds, not 7)."""
    assert result.org_a_cases == 6


def test_cross_tenant_isolation(result) -> None:
    """A different org sees NONE of org A's corrections corpus."""
    assert result.org_b_cases == 0


# --- F-5 / versioning: the winner was frozen -------------------------------------
def test_winner_frozen(result) -> None:
    assert result.frozen_sha


# --- determinism: two runs are bit-identical (replay-by-content-sha) --------------
def test_deterministic_across_runs() -> None:
    """Two independent runs produce the same gate decision and the same loop
    fixed-point content sha — the bit-identical-replay property."""
    module = _load_scenario()
    a = module.run_self_improvement(live=False)
    b = module.run_self_improvement(live=False)
    assert a.gate.promoted == b.gate.promoted
    assert a.loop_fixed_point_sha == b.loop_fixed_point_sha
    assert a.frozen_sha == b.frozen_sha


# --- F-7: the borrow is exclusive ------------------------------------------------
def test_borrow_is_exclusive() -> None:
    """A second concurrent mutable borrow of the same definition is refused."""
    from crawfish.borrow import ExclusiveBorrowError, mutable
    from crawfish.definition import Definition
    from crawfish.store import SqliteStore

    store = SqliteStore()
    defn = Definition.from_package(str(REPO_ROOT / "demo" / "triage-bot"))
    with defn.mutable(store, org_id="acme"):
        with pytest.raises(ExclusiveBorrowError):
            with mutable(defn, store, org_id="acme"):
                pass
    # released on exit -> a fresh borrow now succeeds
    with defn.mutable(store, org_id="acme"):
        pass
    store.close()
