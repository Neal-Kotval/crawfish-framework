"""F-3 / CRA-196 — the gate algebra (single owner of the eval.py gate).

Three reconciled gate notions, all consuming ``crawfish.experiment`` (F-8):
  (a) relative-regression  — ``is_regression`` / ``gate_against_baseline``
  (b) variance-aware paired — ``paired_gate`` (+ aggregate sibling
      ``is_regression_variance_aware``)
  (c) absolute-precision    — ``precision_gate``, fails closed.

The load-bearing back-compat pin: ``std=0, k=0`` reproduces today's
``is_regression`` BYTE-FOR-BYTE.
"""

from __future__ import annotations

import pytest

from crawfish.eval import (
    GateDecision,
    VerifierNotGated,
    paired_gate,
    precision_gate,
)
from crawfish.metrics import (
    is_regression,
    is_regression_variance_aware,
    noise_band,
)


# --------------------------------------------------------------------------- #
# AC1: std=0, k=0 reproduces today's is_regression BYTE-FOR-BYTE.            #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("baseline", "candidate", "tolerance"),
    [
        ({"acc": 0.8, "f1": 0.7}, {"acc": 0.81, "f1": 0.71}, 0.0),  # both up -> no regress
        ({"acc": 0.8, "f1": 0.7}, {"acc": 0.79, "f1": 0.71}, 0.0),  # acc down -> regress
        ({"acc": 0.8}, {"acc": 0.799}, 0.0),  # tiny drop, zero tol -> regress
        ({"acc": 0.8}, {"acc": 0.799}, 0.01),  # tiny drop within tol -> no regress
        ({"acc": 0.8, "new": 0.5}, {"acc": 0.8}, 0.0),  # missing metric treated as 0 drop
        ({}, {}, 0.0),  # empty
        ({"x": 1.0}, {"x": 1.0}, 0.0),  # equal -> no regress
    ],
)
def test_std0_k0_reduces_to_is_regression_byte_for_byte(baseline, candidate, tolerance):
    """With std all-zero (or None) and k collapsed, the variance-aware path must
    return the IDENTICAL bool as today's is_regression for every input."""
    expected = is_regression(baseline, candidate, tolerance=tolerance)

    # std is None ⇒ zero-width band ⇒ must match exactly.
    got_none = is_regression_variance_aware(baseline, candidate, std=None, tolerance=tolerance)
    assert got_none is expected

    # std explicitly 0 for every metric ⇒ k * 0 == 0 ⇒ must match exactly.
    zero_std = {name: 0.0 for name in set(baseline) | set(candidate)}
    got_zero = is_regression_variance_aware(
        baseline, candidate, std=zero_std, alpha=0.05, tolerance=tolerance
    )
    assert got_zero is expected


def test_noise_band_zero_when_std_zero():
    """k is derived from alpha but k * 0 == 0 — the band vanishes at std=0."""
    band = noise_band({"acc": 0.0, "f1": 0.0}, alpha=0.05)
    assert band == {"acc": 0.0, "f1": 0.0}


def test_variance_aware_band_absorbs_small_drop():
    """A drop inside k*std is NOT a regression; the same drop at std=0 IS."""
    baseline = {"acc": 0.80}
    candidate = {"acc": 0.78}  # 0.02 drop
    # std=0 -> regression (today's behaviour).
    assert is_regression_variance_aware(baseline, candidate, std={"acc": 0.0}) is True
    # std large enough that k*std > 0.02 -> absorbed, not a regression.
    assert is_regression_variance_aware(baseline, candidate, std={"acc": 0.05}, alpha=0.05) is False


# --------------------------------------------------------------------------- #
# AC2: a candidate within the paired noise band is REJECTED (CI straddles 0). #
# --------------------------------------------------------------------------- #
def test_paired_gate_rejects_within_noise_band():
    """Per-case deltas centred on ~0 with spread ⇒ CI straddles 0 ⇒ reject."""
    # Equal-and-opposite per-case deltas: mean ~0, CI straddles 0.
    baseline = {"q": [0.5, 0.5, 0.5, 0.5, 0.5, 0.5]}
    candidate = {"q": [0.6, 0.4, 0.6, 0.4, 0.6, 0.4]}  # deltas +/-0.1, mean 0
    decision = paired_gate(baseline, candidate, primary="q", seed=0)
    assert isinstance(decision, GateDecision)
    assert decision.promoted is False
    v = decision.verdicts[0]
    assert v.lo < 0.0 < v.hi  # CI straddles 0


# --------------------------------------------------------------------------- #
# AC5: a clearly-better candidate (CI strictly > 0) is PROMOTED.             #
# --------------------------------------------------------------------------- #
def test_paired_gate_promotes_clearly_better():
    """Every case improves by a consistent margin ⇒ CI strictly above 0 ⇒ promote."""
    baseline = {"q": [0.50, 0.52, 0.48, 0.51, 0.49, 0.50]}
    candidate = {"q": [0.70, 0.72, 0.68, 0.71, 0.69, 0.70]}  # +0.20 each
    decision = paired_gate(baseline, candidate, primary="q", seed=0)
    assert decision.promoted is True
    assert decision.verdicts[0].lo > 0.0


# --------------------------------------------------------------------------- #
# AC3: a rich rubric does NOT inflate false-promotion past alpha (Holm).     #
# --------------------------------------------------------------------------- #
def test_rich_rubric_holm_does_not_inflate_false_promotion():
    """Many noisy metrics + a flat primary: Holm correction must NOT promote on
    the primary just because the rubric is wide (no alpha inflation)."""
    n = 8
    base: dict[str, list[float]] = {}
    cand: dict[str, list[float]] = {}
    # primary is flat (deltas alternate +/-, mean ~0): must not promote.
    base["primary"] = [0.5] * n
    cand["primary"] = [0.5 + (0.1 if i % 2 == 0 else -0.1) for i in range(n)]
    # several other noisy metrics, also flat.
    for m in range(6):
        key = f"noise_{m}"
        base[key] = [0.5] * n
        cand[key] = [0.5 + (0.08 if i % 2 else -0.08) for i in range(n)]
    decision = paired_gate(base, cand, primary="primary", alpha=0.05, seed=0)
    assert decision.promoted is False


def test_holm_promotes_only_genuine_primary_improvement():
    """A genuine primary improvement promotes even amid noisy guardrail metrics."""
    n = 8
    base = {"primary": [0.5] * n}
    cand = {"primary": [0.8] * n}  # strong, consistent +0.3
    for m in range(4):
        key = f"noise_{m}"
        base[key] = [0.5] * n
        cand[key] = [0.5 + (0.05 if i % 2 else -0.05) for i in range(n)]
    decision = paired_gate(base, cand, primary="primary", alpha=0.05, seed=0)
    assert decision.promoted is True


def test_primary_plus_guardrail_design():
    """Primary improves but a guardrail regresses past its margin ⇒ reject."""
    n = 6
    base = {"primary": [0.5] * n, "safety": [0.9] * n}
    cand = {
        "primary": [0.8] * n,  # strong improvement
        "safety": [0.5] * n,  # big drop, breaches a tight margin
    }
    decision = paired_gate(base, cand, primary="primary", guardrails={"safety": 0.05}, seed=0)
    assert decision.promoted is False
    assert "guardrail" in decision.reason

    # With a loose guardrail margin, the same candidate promotes.
    ok = paired_gate(base, cand, primary="primary", guardrails={"safety": 0.9}, seed=0)
    assert ok.promoted is True


def test_paired_gate_deterministic():
    """Identical inputs + seed ⇒ identical decision (bootstrap is seeded)."""
    base = {"q": [0.5, 0.6, 0.55, 0.52, 0.58, 0.54]}
    cand = {"q": [0.7, 0.8, 0.75, 0.72, 0.78, 0.74]}
    d1 = paired_gate(base, cand, primary="q", seed=7)
    d2 = paired_gate(base, cand, primary="q", seed=7)
    assert d1 == d2


def test_paired_gate_unequal_lengths_raises():
    base = {"q": [0.5, 0.5]}
    cand = {"q": [0.6]}
    with pytest.raises(ValueError, match="equal-length"):
        paired_gate(base, cand, primary="q")


def test_paired_gate_missing_primary_raises():
    with pytest.raises(KeyError):
        paired_gate({"a": [0.5]}, {"a": [0.6]}, primary="missing")


# --------------------------------------------------------------------------- #
# AC4: the precision gate RAISES when no baseline exists (fails closed).     #
# --------------------------------------------------------------------------- #
def test_precision_gate_fails_closed_without_baseline():
    """A never-benchmarked verifier is REJECTED by raising — not admitted."""
    decisions = [True, True, True, True]
    labels = [True, True, True, True]  # perfect precision, but...
    with pytest.raises(VerifierNotGated, match="no baseline"):
        precision_gate(decisions, labels, min_precision=0.9, baseline_exists=False)


def test_precision_gate_admits_when_precision_met_and_baseline_exists():
    decisions = [True, True, True, True, False]
    labels = [True, True, True, False, False]  # TP=3, FP=1 -> precision 0.75
    prec = precision_gate(decisions, labels, min_precision=0.75, baseline_exists=True)
    assert prec == pytest.approx(0.75)


def test_precision_gate_rejects_below_min_precision():
    decisions = [True, True, True, True]
    labels = [True, False, False, False]  # TP=1, FP=3 -> precision 0.25
    with pytest.raises(VerifierNotGated, match="below required"):
        precision_gate(decisions, labels, min_precision=0.9, baseline_exists=True)


def test_precision_gate_no_positive_decisions_fails_closed():
    """No positive predictions ⇒ precision undefined ⇒ fail closed (raise)."""
    decisions = [False, False, False]
    labels = [True, False, True]
    with pytest.raises(VerifierNotGated, match="no positive decisions"):
        precision_gate(decisions, labels, min_precision=0.5, baseline_exists=True)


def test_precision_gate_misaligned_raises():
    with pytest.raises(ValueError, match="aligned"):
        precision_gate([True, False], [True], min_precision=0.5, baseline_exists=True)
