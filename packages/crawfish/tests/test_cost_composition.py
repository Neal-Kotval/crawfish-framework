"""F-6 / CRA-199 acceptance: the multiplicative operator-cost composition law.

`cost.py` is the single owner of the cost model. These tests pin the contract
that CL-3 and ALG-5 consume as readers:

* `total_usd` is the unchanged lower bound.
* `worst_case = lower × Π(per-operator multiplier)`, multiplicative along the
  operator nesting; escalation is re-priced on the strong model.
* with no measured rates, `expected == worst_case` (never undercount).
* with measured rates, `expected` is a band with `lower <= expected <= worst`.
* `recurse` contributes `b ** max_depth`.
"""

from __future__ import annotations

import pytest

from crawfish.cost import CostEstimate, CostShape, compose_cost, estimate_cost
from crawfish.definition.types import AgentSpec, TeamSpec
from crawfish.definition.types import Definition as Def


def _base(usd: float = 10.0) -> CostEstimate:
    """A bare base estimate with `total_usd == usd` and no operator wrappers."""
    return CostEstimate(
        team_size=1,
        items=1,
        per_item_usd=usd,
        total_usd=usd,
        per_model={"base": usd},
    )


# -- AC#1: total_usd semantics unchanged ------------------------------------
def test_bare_estimate_is_a_degenerate_interval() -> None:
    """A CostEstimate built without operator fields collapses to a point."""
    est = _base(10.0)
    assert est.total_usd == 10.0
    # additive fields default to the lower bound -> degenerate interval.
    assert est.worst_case_usd == 10.0
    assert est.expected_usd == 10.0
    assert est.expected_lo_usd == 10.0
    assert est.expected_hi_usd == 10.0


def test_estimate_cost_fills_interval_lower_bound() -> None:
    """`estimate_cost` (existing API) yields a well-formed interval where all
    three numbers equal the lower bound (no operator wrappers known to it)."""
    d = Def(team=TeamSpec(agents=[AgentSpec(role="a", model="x")]))
    est = estimate_cost(d, items=2, model_prices={"x": 1.5})
    assert est.total_usd == pytest.approx(3.0)
    assert est.worst_case_usd == pytest.approx(3.0)
    assert est.expected_usd == pytest.approx(3.0)


def test_compose_with_no_shapes_is_identity() -> None:
    est = compose_cost(_base(7.0), [])
    assert est.total_usd == 7.0
    assert est.worst_case_usd == 7.0
    assert est.expected_usd == 7.0


# -- AC#2: multiplicative composition + nesting order -----------------------
def test_refine_times_quorum_is_multiplicative() -> None:
    """refine(max_iters=3) wrapping quorum(k=5) over a base => base × 3 × 5."""
    base = _base(2.0)
    shapes = [CostShape.refine(max_iters=3), CostShape.quorum(5)]
    est = compose_cost(base, shapes)
    assert est.total_usd == pytest.approx(2.0)  # lower bound untouched
    assert est.worst_case_usd == pytest.approx(2.0 * 3 * 5)


def test_nesting_order_is_commutative_for_worst_case() -> None:
    """The product is order-independent (it's a fold of multipliers)."""
    base = _base(2.0)
    a = compose_cost(base, [CostShape.refine(max_iters=3), CostShape.quorum(5)])
    b = compose_cost(base, [CostShape.quorum(5), CostShape.refine(max_iters=3)])
    assert a.worst_case_usd == pytest.approx(b.worst_case_usd)


def test_escalate_repriced_on_strong_model() -> None:
    """Escalation worst case = base call + one strong-model attempt, priced on
    the strong model's per-call cost (not a flat 2× of the base)."""
    base = _base(1.0)  # base call costs $1
    # strong model is 5× the base per-call price.
    shape = CostShape.escalate(base_price=1.0, strong_price=5.0)
    est = compose_cost(base, [shape])
    # worst = base(1) + strong(5) = 6, NOT 2 × base = 2.
    assert est.worst_case_usd == pytest.approx(6.0)


def test_escalate_count_fallback_when_base_free() -> None:
    """A zero base price falls back to the count-based 2× so worst is defined."""
    base = _base(4.0)
    shape = CostShape.escalate(base_price=0.0, strong_price=5.0)
    est = compose_cost(base, [shape])
    # strong_multiplier falls back to 1.0 => factor 1 + 1 = 2.
    assert est.worst_case_usd == pytest.approx(8.0)


def test_full_nested_refine_escalate_quorum_product() -> None:
    """OPT-2 worked example: Refine(4) ∘ Escalate(2×) ∘ Quorum(5) => 40× when the
    escalation strong attempt is priced equal to the base call."""
    base = _base(1.0)
    shapes = [
        CostShape.refine(max_iters=4),
        CostShape.escalate(base_price=1.0, strong_price=1.0),  # 2× factor
        CostShape.quorum(5),
    ]
    est = compose_cost(base, shapes)
    assert est.worst_case_usd == pytest.approx(1.0 * 4 * 2 * 5)  # == 40


# -- AC#3: no measured rates => expected == worst_case ----------------------
def test_no_rates_expected_equals_worst_case() -> None:
    base = _base(2.0)
    shapes = [CostShape.refine(max_iters=3), CostShape.quorum(5)]
    est = compose_cost(base, shapes)
    assert est.expected_usd == pytest.approx(est.worst_case_usd)
    assert est.expected_lo_usd == pytest.approx(est.worst_case_usd)
    assert est.expected_hi_usd == pytest.approx(est.worst_case_usd)


# -- AC#4: with measured rates, expected is a band within [lower, worst] -----
def test_measured_rate_yields_band_between_bounds() -> None:
    base = _base(1.0)
    # escalation fires only 20% of the time; strong attempt costs 1× the base.
    shape = CostShape.escalate(base_price=1.0, strong_price=1.0, measured_rate=0.2, rate_ci=0.05)
    est = compose_cost(base, [shape])
    worst = 2.0  # 1 base + 1 strong
    lower = 1.0
    # expected = 1 + 0.2 × (2 - 1) = 1.2, strictly inside (lower, worst).
    assert est.expected_usd == pytest.approx(1.2)
    assert lower < est.expected_usd < worst
    # the CI makes it a band, not a point.
    assert est.expected_lo_usd < est.expected_usd < est.expected_hi_usd
    # band stays inside the hard interval.
    assert est.expected_lo_usd >= est.total_usd
    assert est.expected_hi_usd <= est.worst_case_usd


def test_expected_bounded_below_by_lower_and_above_by_worst() -> None:
    base = _base(3.0)
    shapes = [
        CostShape.refine(max_iters=4, measured_rate=0.5, rate_ci=0.1),
        CostShape.retry(3, measured_rate=0.1, rate_ci=0.2),
    ]
    est = compose_cost(base, shapes)
    assert est.total_usd <= est.expected_lo_usd <= est.expected_usd
    assert est.expected_usd <= est.expected_hi_usd <= est.worst_case_usd
    # lower bound is the floor (every operator fires once).
    assert est.expected_usd >= est.total_usd


def test_zero_rate_collapses_expected_to_lower_bound() -> None:
    """A measured rate of 0 means the extra work never fires -> expected == lower."""
    base = _base(5.0)
    shape = CostShape.refine(max_iters=10, measured_rate=0.0)
    est = compose_cost(base, [shape])
    assert est.expected_usd == pytest.approx(5.0)  # the lower bound
    assert est.worst_case_usd == pytest.approx(50.0)


# -- AC#5: recurse contributes b^max_depth ----------------------------------
def test_recurse_contributes_branching_to_the_depth() -> None:
    base = _base(1.0)
    shape = CostShape.recurse(branching=3, max_depth=4)  # 3**4 = 81
    est = compose_cost(base, [shape])
    assert est.worst_case_usd == pytest.approx(81.0)


def test_recurse_depth_zero_is_one_call() -> None:
    est = compose_cost(_base(2.0), [CostShape.recurse(branching=5, max_depth=0)])
    assert est.worst_case_usd == pytest.approx(2.0)  # 5**0 == 1


# -- determinism / purity ---------------------------------------------------
def test_compose_is_pure_and_deterministic() -> None:
    base = _base(2.5)
    shapes = [CostShape.refine(max_iters=3), CostShape.quorum(4)]
    a = compose_cost(base, shapes)
    b = compose_cost(base, shapes)
    assert a.model_dump() == b.model_dump()
    # base is untouched (frozen value, fresh result).
    assert base.worst_case_usd == 2.5


# -- guard rails ------------------------------------------------------------
def test_bad_multipliers_raise() -> None:
    with pytest.raises(ValueError):
        CostShape.refine(max_iters=0)
    with pytest.raises(ValueError):
        CostShape.quorum(0)
    with pytest.raises(ValueError):
        CostShape.retry(0)
    with pytest.raises(ValueError):
        CostShape.recurse(branching=0, max_depth=2)


def test_malformed_interval_rejected() -> None:
    """A directly-minted estimate that violates the ordering invariant raises."""
    with pytest.raises(ValueError):
        CostEstimate(
            team_size=1,
            items=1,
            per_item_usd=1.0,
            total_usd=10.0,
            # worst_case below the lower bound -> invalid interval.
            worst_case_usd=5.0,
        )
