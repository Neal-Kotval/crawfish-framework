"""Acceptance tests for the F-8 shared statistical primitives (CRA-201).

Every test asserts the purity/determinism contract (same inputs + seed ->
identical output) alongside the statistical behaviour the gate algebra relies on.
"""

from __future__ import annotations

import math

import pytest

from crawfish.experiment import (
    anytime_valid_bound,
    holm_correction,
    k_from_alpha,
    min_detectable_effect,
    normal_cdf,
    normal_ppf,
    paired_bootstrap_ci,
    required_n,
    tune_gate_split,
    winners_curse_shrink,
)


# --------------------------------------------------------------------------- #
# normal_ppf / normal_cdf                                                      #
# --------------------------------------------------------------------------- #
def test_normal_ppf_known_quantiles() -> None:
    assert normal_ppf(0.5) == pytest.approx(0.0, abs=1e-9)
    assert normal_ppf(0.975) == pytest.approx(1.959963985, abs=1e-6)
    assert normal_ppf(0.95) == pytest.approx(1.644853627, abs=1e-6)


def test_normal_ppf_cdf_roundtrip() -> None:
    for p in (0.01, 0.2, 0.5, 0.8, 0.99):
        assert normal_cdf(normal_ppf(p)) == pytest.approx(p, abs=1e-6)


def test_normal_ppf_rejects_out_of_range() -> None:
    with pytest.raises(ValueError):
        normal_ppf(0.0)
    with pytest.raises(ValueError):
        normal_ppf(1.0)


# --------------------------------------------------------------------------- #
# paired_bootstrap_ci                                                          #
# --------------------------------------------------------------------------- #
def test_paired_bootstrap_ci_deterministic() -> None:
    deltas = [0.1, -0.2, 0.05, 0.3, -0.1, 0.2, 0.0, 0.15]
    a = paired_bootstrap_ci(deltas, alpha=0.05, n_resamples=1000, seed=7)
    b = paired_bootstrap_ci(deltas, alpha=0.05, n_resamples=1000, seed=7)
    assert a == b


def test_paired_bootstrap_ci_noise_band_straddles_zero() -> None:
    # Candidate within the paired noise band: symmetric deltas about ~0.
    deltas = [0.1, -0.1, 0.05, -0.05, 0.12, -0.11, 0.0, 0.03, -0.04, 0.02]
    lo, hi, mean = paired_bootstrap_ci(deltas, alpha=0.05, n_resamples=2000, seed=1)
    assert lo < 0.0 < hi  # straddles 0 -> a gate would reject
    assert lo <= mean <= hi


def test_paired_bootstrap_ci_clear_winner_strictly_positive() -> None:
    # Clearly-better candidate: every per-case delta is solidly positive.
    deltas = [0.2, 0.25, 0.18, 0.22, 0.3, 0.19, 0.21, 0.27, 0.24, 0.2]
    lo, hi, mean = paired_bootstrap_ci(deltas, alpha=0.05, n_resamples=2000, seed=1)
    assert lo > 0.0  # CI strictly above 0 -> gate adopts
    assert mean > 0.0


def test_paired_bootstrap_ci_single_delta_degenerate() -> None:
    assert paired_bootstrap_ci([0.42], seed=3) == (0.42, 0.42, 0.42)


def test_paired_bootstrap_ci_rejects_empty() -> None:
    with pytest.raises(ValueError):
        paired_bootstrap_ci([])


# --------------------------------------------------------------------------- #
# holm_correction                                                             #
# --------------------------------------------------------------------------- #
def test_holm_textbook_pattern() -> None:
    # Classic worked example at alpha=0.05, m=4.
    # sorted p: 0.005, 0.011, 0.02, 0.04  vs thresholds 0.0125,0.0167,0.025,0.05
    # step-down: 0.005<=0.0125 reject; 0.011<=0.0167 reject; 0.02<=0.025 reject;
    #            0.04<=0.05 reject -> all four reject.
    pvals = [0.04, 0.005, 0.02, 0.011]
    assert holm_correction(pvals, alpha=0.05) == [True, True, True, True]


def test_holm_stops_at_first_failure() -> None:
    # sorted: 0.001, 0.04, 0.05 ; thresholds 0.0167, 0.025, 0.05
    # 0.001<=0.0167 reject; 0.04>0.025 stop -> remaining not rejected.
    pvals = [0.001, 0.04, 0.05]
    assert holm_correction(pvals, alpha=0.05) == [True, False, False]


def test_holm_no_rejections() -> None:
    pvals = [0.3, 0.6, 0.9]
    assert holm_correction(pvals, alpha=0.05) == [False, False, False]


def test_holm_empty() -> None:
    assert holm_correction([]) == []


def test_holm_deterministic() -> None:
    pvals = [0.01, 0.02, 0.2, 0.04]
    assert holm_correction(pvals) == holm_correction(pvals)


# --------------------------------------------------------------------------- #
# k_from_alpha                                                                #
# --------------------------------------------------------------------------- #
def test_k_from_alpha_two_sided() -> None:
    assert k_from_alpha(0.05) == pytest.approx(1.959963985, abs=1e-6)


def test_k_from_alpha_one_sided() -> None:
    assert k_from_alpha(0.05, two_sided=False) == pytest.approx(1.644853627, abs=1e-6)


def test_k_from_alpha_monotone() -> None:
    # Tighter alpha -> wider band -> larger k.
    assert k_from_alpha(0.01) > k_from_alpha(0.05) > k_from_alpha(0.20)


def test_k_from_alpha_deterministic() -> None:
    assert k_from_alpha(0.05) == k_from_alpha(0.05)


# --------------------------------------------------------------------------- #
# tune_gate_split                                                             #
# --------------------------------------------------------------------------- #
def test_tune_gate_split_disjoint_and_complete() -> None:
    cases = list(range(20))
    tune, gate = tune_gate_split(cases, frac=0.5, seed=11)
    assert set(tune).isdisjoint(set(gate))
    assert sorted(tune + gate) == cases
    assert len(tune) == 10


def test_tune_gate_split_reproducible() -> None:
    cases = list(range(50))
    a = tune_gate_split(cases, frac=0.3, seed=99)
    b = tune_gate_split(cases, frac=0.3, seed=99)
    assert a == b


def test_tune_gate_split_seed_changes_partition() -> None:
    cases = list(range(50))
    a = tune_gate_split(cases, frac=0.5, seed=1)
    b = tune_gate_split(cases, frac=0.5, seed=2)
    assert a != b


def test_tune_gate_split_frac_extremes() -> None:
    cases = list(range(10))
    all_tune, no_gate = tune_gate_split(cases, frac=1.0, seed=0)
    assert sorted(all_tune) == cases and no_gate == []
    no_tune, all_gate = tune_gate_split(cases, frac=0.0, seed=0)
    assert no_tune == [] and sorted(all_gate) == cases


# --------------------------------------------------------------------------- #
# winners_curse_shrink                                                        #
# --------------------------------------------------------------------------- #
def test_winners_curse_shrinks_toward_fresh() -> None:
    # Inflated selection score 0.92, honest fresh estimate 0.80.
    shrunk = winners_curse_shrink(0.92, 0.80, weight=0.5)
    assert 0.80 < shrunk < 0.92
    assert shrunk == pytest.approx(0.86)


def test_winners_curse_full_shrinkage_is_fresh() -> None:
    assert winners_curse_shrink(0.92, 0.80) == pytest.approx(0.80)


def test_winners_curse_no_shrinkage_keeps_argmax() -> None:
    assert winners_curse_shrink(0.92, 0.80, weight=0.0) == pytest.approx(0.92)


def test_winners_curse_deterministic() -> None:
    assert winners_curse_shrink(0.9, 0.7, weight=0.4) == winners_curse_shrink(0.9, 0.7, weight=0.4)


# --------------------------------------------------------------------------- #
# power helpers                                                               #
# --------------------------------------------------------------------------- #
def test_mde_and_required_n_are_inverses() -> None:
    std = 0.2
    n = 64
    mde = min_detectable_effect(n, std, alpha=0.05, power=0.8)
    assert mde > 0
    # required_n for that exact mde should be ~n (ceil may bump by 1).
    assert required_n(mde, std, alpha=0.05, power=0.8) in (n, n + 1)


def test_mde_shrinks_with_more_data() -> None:
    assert min_detectable_effect(100, 0.2) < min_detectable_effect(25, 0.2)


def test_mde_zero_n_is_infinite() -> None:
    assert math.isinf(min_detectable_effect(0, 0.2))


def test_required_n_zero_std_is_one() -> None:
    assert required_n(0.05, 0.0) == 1


def test_required_n_rejects_nonpositive_mde() -> None:
    with pytest.raises(ValueError):
        required_n(0.0, 0.2)


def test_required_n_grows_for_smaller_effect() -> None:
    assert required_n(0.01, 0.2) > required_n(0.1, 0.2)


# --------------------------------------------------------------------------- #
# anytime_valid_bound                                                         #
# --------------------------------------------------------------------------- #
def test_anytime_valid_bound_brackets_mean() -> None:
    (
        lo,
        hi,
    ) = anytime_valid_bound(0.1, 0.2, 50, alpha=0.05)
    assert lo < 0.1 < hi


def test_anytime_valid_tighter_than_naive_is_wider() -> None:
    # Anytime-valid bound must be WIDER than a fixed-n normal CI (the price of
    # peeking control): half-width should exceed z*std/sqrt(n).
    mean, std, n, alpha = 0.0, 0.2, 50, 0.05
    lo, hi = anytime_valid_bound(mean, std, n, alpha=alpha)
    half = (hi - lo) / 2.0
    fixed_half = normal_ppf(1 - alpha / 2) * std / math.sqrt(n)
    assert half > fixed_half


def test_anytime_valid_zero_n_is_infinite() -> None:
    assert anytime_valid_bound(0.0, 0.2, 0) == (-math.inf, math.inf)


def test_anytime_valid_deterministic() -> None:
    assert anytime_valid_bound(0.1, 0.2, 30) == anytime_valid_bound(0.1, 0.2, 30)
