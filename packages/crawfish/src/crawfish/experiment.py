"""Shared statistical primitives for the Crawfish gate algebra (F-8 / CRA-201).

This module is the **single statistical substrate** that every consumer of the
gate algebra (``calibrate`` / ``gate`` / ``quorum`` / ``explore`` / ``guard``)
imports. The design contract these primitives implement is specified in
``docs/architecture/experiment-design.md``; that document is a cross-cutting
acceptance gate — no gate ships until it conforms.

**Purity / determinism contract (mandatory).** Every function here is pure: no
model calls, no I/O, no wall-clock, no global/process-level randomness. Any
randomness (e.g. the bootstrap resample) is driven by an explicit ``seed`` and a
local ``random.Random`` instance, never the module-global ``random`` state.
Given identical inputs (including ``seed``) every function returns byte-for-byte
identical output across calls and across processes.

Dependency note: the repo ships no ``numpy``/``scipy`` (see ``pyproject.toml`` —
only ``pydantic``/``typing-extensions``/``pyyaml``). These primitives therefore
use the standard library only: ``math``, ``statistics`` and a seeded
``random.Random``.
"""

from __future__ import annotations

import math
import random
import statistics
from collections.abc import Sequence

__all__ = [
    "paired_bootstrap_ci",
    "holm_correction",
    "k_from_alpha",
    "tune_gate_split",
    "winners_curse_shrink",
    "min_detectable_effect",
    "required_n",
    "anytime_valid_bound",
    "normal_ppf",
    "normal_cdf",
]


# --------------------------------------------------------------------------- #
# Normal-distribution helpers (stdlib-only, deterministic).                    #
# --------------------------------------------------------------------------- #
def normal_cdf(z: float) -> float:
    """Standard-normal CDF ``Phi(z)`` via the error function (deterministic)."""
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def normal_ppf(p: float) -> float:
    """Standard-normal inverse CDF (quantile) ``Phi^-1(p)`` for ``0 < p < 1``.

    Uses the Acklam rational approximation (max abs error ~1.15e-9 in the
    relative-error sense), which is fully deterministic and stdlib-only. We
    deliberately avoid ``statistics.NormalDist().inv_cdf`` differences across
    interpreter versions by pinning our own implementation so the noise-band
    multiplier ``k`` is reproducible byte-for-byte.
    """
    if not 0.0 < p < 1.0:
        raise ValueError(f"normal_ppf requires 0 < p < 1, got {p!r}")
    # Coefficients for Acklam's algorithm.
    a = [
        -3.969683028665376e01,
        2.209460984245205e02,
        -2.759285104469687e02,
        1.383577518672690e02,
        -3.066479806614716e01,
        2.506628277459239e00,
    ]
    b = [
        -5.447609879822406e01,
        1.615858368580409e02,
        -1.556989798598866e02,
        6.680131188771972e01,
        -1.328068155288572e01,
    ]
    c = [
        -7.784894002430293e-03,
        -3.223964580411365e-01,
        -2.400758277161838e00,
        -2.549732539343734e00,
        4.374664141464968e00,
        2.938163982698783e00,
    ]
    d = [
        7.784695709041462e-03,
        3.224671290700398e-01,
        2.445134137142996e00,
        3.754408661907416e00,
    ]
    p_low = 0.02425
    p_high = 1.0 - p_low
    if p < p_low:
        q = math.sqrt(-2.0 * math.log(p))
        x = (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
            (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0
        )
    elif p <= p_high:
        q = p - 0.5
        r = q * q
        x = (
            (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5])
            * q
            / (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1.0)
        )
    else:
        q = math.sqrt(-2.0 * math.log(1.0 - p))
        x = -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
            (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0
        )
    return x


# --------------------------------------------------------------------------- #
# Paired bootstrap CI on the mean per-case delta.                             #
# --------------------------------------------------------------------------- #
def paired_bootstrap_ci(
    deltas: Sequence[float],
    alpha: float = 0.05,
    n_resamples: int = 2000,
    seed: int = 0,
) -> tuple[float, float, float]:
    """Paired percentile bootstrap CI for the **mean per-case delta**.

    ``deltas[i]`` is the per-case score difference ``candidate_i - baseline_i``
    over the *same* ``GoldenSet`` case ``i`` (this is the paired design: baseline
    and candidate are scored on identical cases, so the per-case difference
    cancels case difficulty — see experiment-design.md §"Paired tests").

    Returns ``(lo, hi, mean)`` where ``[lo, hi]`` is the two-sided
    ``1 - alpha`` percentile-bootstrap CI for the mean of ``deltas`` and ``mean``
    is the observed point estimate. A gate adopts a candidate only when the CI
    lies strictly above 0 (``lo > 0``); a CI that straddles 0 is "within the
    noise band" and is rejected.

    Determinism: the resample indices are drawn from a local
    ``random.Random(seed)`` — never the global RNG — so identical
    ``(deltas, alpha, n_resamples, seed)`` always yields identical output.
    """
    if not 0.0 < alpha < 1.0:
        raise ValueError(f"alpha must be in (0, 1), got {alpha!r}")
    if n_resamples < 1:
        raise ValueError(f"n_resamples must be >= 1, got {n_resamples!r}")
    n = len(deltas)
    if n == 0:
        raise ValueError("paired_bootstrap_ci requires at least one delta")
    data = [float(x) for x in deltas]
    mean = statistics.fmean(data)
    if n == 1:
        # No resampling variance available; CI degenerates to the point.
        return (mean, mean, mean)

    rng = random.Random(seed)
    means: list[float] = []
    for _ in range(n_resamples):
        total = 0.0
        for _ in range(n):
            total += data[rng.randrange(n)]
        means.append(total / n)
    means.sort()

    lo = _percentile(means, 100.0 * (alpha / 2.0))
    hi = _percentile(means, 100.0 * (1.0 - alpha / 2.0))
    return (lo, hi, mean)


def _percentile(sorted_values: list[float], pct: float) -> float:
    """Linear-interpolated percentile of an already-sorted list (pct in 0..100)."""
    if not sorted_values:
        raise ValueError("percentile of empty sequence")
    if len(sorted_values) == 1:
        return sorted_values[0]
    rank = (pct / 100.0) * (len(sorted_values) - 1)
    lo_idx = math.floor(rank)
    hi_idx = math.ceil(rank)
    if lo_idx == hi_idx:
        return sorted_values[lo_idx]
    frac = rank - lo_idx
    return sorted_values[lo_idx] * (1.0 - frac) + sorted_values[hi_idx] * frac


# --------------------------------------------------------------------------- #
# Holm-Bonferroni family-wise correction.                                     #
# --------------------------------------------------------------------------- #
def holm_correction(pvalues: Sequence[float], alpha: float = 0.05) -> list[bool]:
    """Holm-Bonferroni step-down family-wise reject decisions.

    Controls the family-wise error rate at ``alpha`` across the ``len(pvalues)``
    hypotheses (e.g. one per Rubric metric). Returns a list of booleans aligned
    with the *input order*: ``True`` means "reject the null / the effect is
    significant for that metric".

    Procedure: sort p-values ascending; the ``i``-th smallest (0-based) is
    compared against ``alpha / (m - i)``. The first p-value that fails to clear
    its threshold stops the step-down — it and all larger p-values are not
    rejected.

    Determinism: pure arithmetic, stable for ties (input order preserved via a
    stable sort key).
    """
    if not 0.0 < alpha < 1.0:
        raise ValueError(f"alpha must be in (0, 1), got {alpha!r}")
    m = len(pvalues)
    if m == 0:
        return []
    indexed = sorted(range(m), key=lambda i: pvalues[i])
    reject = [False] * m
    stopped = False
    for rank, idx in enumerate(indexed):
        threshold = alpha / (m - rank)
        if not stopped and pvalues[idx] <= threshold:
            reject[idx] = True
        else:
            stopped = True
    return reject


# --------------------------------------------------------------------------- #
# Noise-band multiplier from a stated alpha.                                   #
# --------------------------------------------------------------------------- #
def k_from_alpha(alpha: float = 0.05, *, two_sided: bool = True) -> float:
    """Noise-band multiplier ``k`` derived from a stated significance level.

    The legacy gate compared a candidate against ``baseline - k * std``. Rather
    than leave ``k`` a free magic constant, it is the standard-normal quantile of
    the chosen significance level (experiment-design.md §"k from alpha"):

    * two-sided: ``k = Phi^-1(1 - alpha/2)`` (e.g. ``alpha=0.05`` -> ``1.95996``)
    * one-sided: ``k = Phi^-1(1 - alpha)``   (e.g. ``alpha=0.05`` -> ``1.64485``)

    For ``alpha`` -> 1 the band collapses (``k`` -> 0), recovering the original
    zero-tolerance ``is_regression`` behaviour at ``alpha = 1`` (``k = 0``); this
    is how F-3 reproduces today's byte-for-byte gate with ``std=0, k=0``.
    """
    if not 0.0 < alpha < 1.0:
        raise ValueError(f"alpha must be in (0, 1), got {alpha!r}")
    p = 1.0 - (alpha / 2.0 if two_sided else alpha)
    return normal_ppf(p)


# --------------------------------------------------------------------------- #
# Held-out tune/gate split.                                                    #
# --------------------------------------------------------------------------- #
def tune_gate_split(
    cases: Sequence[object],
    frac: float = 0.5,
    seed: int = 0,
) -> tuple[list[object], list[object]]:
    """Deterministically split ``cases`` into a tune set and a held-out gate set.

    The Tuner searches over the *tune* set; the gate then evaluates the chosen
    candidate on the *gate* set it never saw — closing the optimizer-overfits-
    the-eval hole (experiment-design.md §"Tune/gate split"). ``frac`` is the
    fraction routed to the tune set.

    Guarantees: the two lists are **disjoint**, together cover **every** input
    case exactly once, and the partition is fully **reproducible** for a given
    ``(cases, frac, seed)``. Order within each side is deterministic (sorted by
    the seeded permutation), independent of the global RNG.
    """
    if not 0.0 <= frac <= 1.0:
        raise ValueError(f"frac must be in [0, 1], got {frac!r}")
    items = list(cases)
    n = len(items)
    rng = random.Random(seed)
    order = list(range(n))
    rng.shuffle(order)
    n_tune = int(round(frac * n))
    tune_idx = set(order[:n_tune])
    tune = [items[i] for i in range(n) if i in tune_idx]
    gate = [items[i] for i in range(n) if i not in tune_idx]
    return (tune, gate)


# --------------------------------------------------------------------------- #
# Winner's-curse shrinkage.                                                    #
# --------------------------------------------------------------------------- #
def winners_curse_shrink(
    argmax_score: float,
    fresh_sample_score: float,
    *,
    weight: float = 1.0,
) -> float:
    """De-bias a promoted ``argmax`` score before it becomes the new baseline.

    Selecting the *maximum* over many candidates inflates the winner's measured
    score (winner's curse / optimism bias). Before storing the promoted score as
    the baseline — which the next round must beat — we re-estimate it on a
    **fresh, independent** sample and shrink the inflated selection score toward
    that unbiased estimate (experiment-design.md §"Winner's-curse correction").

    With ``weight == 1.0`` (default) the de-biased score is exactly the fresh
    estimate (full shrinkage — the unbiased value). ``weight == 0.0`` keeps the
    inflated selection score (no shrinkage). Intermediate values interpolate:
    ``(1 - weight) * argmax_score + weight * fresh_sample_score``. The result
    never exceeds ``argmax_score`` when ``fresh_sample_score <= argmax_score``,
    so the bar cannot ratchet up on selection noise.
    """
    if not 0.0 <= weight <= 1.0:
        raise ValueError(f"weight must be in [0, 1], got {weight!r}")
    return (1.0 - weight) * argmax_score + weight * fresh_sample_score


# --------------------------------------------------------------------------- #
# Power / minimum-detectable-effect helpers (two-sample, equal-n, normal).     #
# --------------------------------------------------------------------------- #
def min_detectable_effect(
    n: int,
    std: float,
    alpha: float = 0.05,
    power: float = 0.8,
) -> float:
    """Minimum detectable effect (MDE) for a paired/one-sample mean test.

    Smallest true mean delta that a sample of ``n`` paired cases with per-case
    delta standard deviation ``std`` can detect with probability ``power`` at a
    two-sided significance level ``alpha``:

        ``MDE = (z_{1-alpha/2} + z_{power}) * std / sqrt(n)``

    Used for GoldenSet sizing guidance (experiment-design.md §"Power / MDE").
    Returns ``inf`` for ``n == 0`` (nothing is detectable with no data).
    """
    if n < 0:
        raise ValueError(f"n must be >= 0, got {n!r}")
    if std < 0:
        raise ValueError(f"std must be >= 0, got {std!r}")
    if n == 0:
        return math.inf
    z_alpha = normal_ppf(1.0 - alpha / 2.0)
    z_power = normal_ppf(power)
    return (z_alpha + z_power) * std / math.sqrt(n)


def required_n(
    mde: float,
    std: float,
    alpha: float = 0.05,
    power: float = 0.8,
) -> int:
    """Required GoldenSet size to detect effect ``mde`` (inverse of MDE).

        ``n = ceil( ((z_{1-alpha/2} + z_{power}) * std / mde)^2 )``

    Returns the smallest integer ``n`` (>= 1) that gives at least ``power`` to
    detect a true mean delta of ``mde`` at two-sided level ``alpha``. ``std == 0``
    needs only one case (``n = 1``); ``mde <= 0`` is undetectable -> raises.
    """
    if mde <= 0:
        raise ValueError(f"mde must be > 0, got {mde!r}")
    if std < 0:
        raise ValueError(f"std must be >= 0, got {std!r}")
    if std == 0:
        return 1
    z_alpha = normal_ppf(1.0 - alpha / 2.0)
    z_power = normal_ppf(power)
    n = ((z_alpha + z_power) * std / mde) ** 2
    return max(1, math.ceil(n))


# --------------------------------------------------------------------------- #
# Anytime-valid confidence sequence (optional-stopping control).              #
# --------------------------------------------------------------------------- #
def anytime_valid_bound(
    mean: float,
    std: float,
    n: int,
    alpha: float = 0.05,
    *,
    rho: float = 1.0,
) -> tuple[float, float]:
    """Anytime-valid confidence sequence half-width around ``mean``.

    Online improvement loops *peek* at the running estimate and may stop early;
    a fixed-n CI loses its coverage guarantee under such optional stopping. A
    confidence sequence holds simultaneously at every sample size, so a loop can
    stop the moment the bound clears 0 without inflating the error past ``alpha``
    (experiment-design.md §"Anytime-valid sequential bounds").

    This uses a normal-mixture (Robbins) sub-Gaussian boundary:

        ``half = std * sqrt( ((n*rho + 1)/n^2) * ln( sqrt(n*rho + 1) / alpha ) )``

    Returns ``(lo, hi) = (mean - half, mean + half)``. ``rho`` tunes the
    mixture (the sample size at which the boundary is tightest). Deterministic
    given its inputs. Returns ``(-inf, inf)`` for ``n == 0``.
    """
    if not 0.0 < alpha < 1.0:
        raise ValueError(f"alpha must be in (0, 1), got {alpha!r}")
    if std < 0:
        raise ValueError(f"std must be >= 0, got {std!r}")
    if rho <= 0:
        raise ValueError(f"rho must be > 0, got {rho!r}")
    if n < 0:
        raise ValueError(f"n must be >= 0, got {n!r}")
    if n == 0:
        return (-math.inf, math.inf)
    nr1 = n * rho + 1.0
    half = std * math.sqrt((nr1 / (n * n)) * math.log(math.sqrt(nr1) / alpha))
    return (mean - half, mean + half)
