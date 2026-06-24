# Experiment-design spec (F-8 / CRA-201)

**Status:** normative. This document is a **cross-cutting acceptance gate**: no
statistical gate ships in the Agent-Language epic until it conforms to this spec,
and every statistical consumer — `calibrate`, `gate` (F-3), `quorum`, `explore`,
`guard` — **must cite this document** in its design and import its primitives
from [`crawfish.experiment`](../../packages/crawfish/src/crawfish/experiment.py).

The primitives are pure (no model calls, no I/O, no wall-clock, no global
randomness; any bootstrap is seeded explicitly), so every decision is a
deterministic post-hoc analysis of recorded runs.

---

## 1. Estimands (what we are actually estimating)

A *gate* answers one question: **did this candidate Definition improve the thing
we care about, beyond noise, on data the optimizer did not see?** State the
estimand before measuring:

- **Primary estimand:** the **mean per-case score delta** `E[candidate_i −
  baseline_i]` over the GoldenSet, where case `i` is scored by both the baseline
  and the candidate. This is a *paired* contrast (§4).
- **Guardrail estimands:** for each non-primary metric, the mean per-case delta,
  evaluated as a **non-inferiority** bound (must not drop more than a
  pre-registered margin), not as an improvement target.
- **Calibration estimands** (`calibrate`): Brier score / NLL as the **primary**
  calibration quantities; ECE is a **diagnostic** reported with a bootstrap CI,
  never the gate criterion. Clopper-Pearson intervals are reserved for genuinely
  **binary** pass/fail metrics and are **dropped** for continuous `[0,1]`
  rubrics.

Each consumer records, alongside its decision, which estimand it used and the
metric it designated primary.

## 2. Primary vs guardrail metrics

A rich rubric tests many metrics at once; testing all of them as improvement
targets inflates the false-promotion rate. Two permitted designs:

1. **One primary + pre-registered guardrails.** Designate exactly one metric as
   the improvement target; every other metric is a non-inferiority guardrail
   with a stated margin. The gate adopts iff the primary improves (CI strictly
   above 0) **and** no guardrail breaches its margin.
2. **Family-wise correction.** Test several metrics jointly and apply Holm
   correction (§5) so the family-wise error rate stays at `α`.

A consumer picks one design and pre-registers it; it may not switch designs
after seeing results.

## 3. Sample sizes: pre-registered or anytime-valid

Two permitted regimes for controlling optional-stopping / peeking error:

- **Pre-registered fixed `n`.** Fix the GoldenSet size *before* looking, sized by
  the power analysis in §7. Evaluate once at that `n`; do not peek-and-stop.
- **Anytime-valid sequential bounds.** Online improvement loops that watch a
  running estimate and may stop early **must** use a confidence sequence
  (`anytime_valid_bound`, §8), which holds simultaneously at every sample size.
  A loop may stop the instant the sequence clears 0 without inflating error past
  `α`. Using a fixed-`n` CI while peeking is **forbidden** — it loses coverage.

## 4. Paired tests over GoldenSet cases

Baseline and candidate are scored on the **same** GoldenSet cases, so the natural
test is **paired**: compute the per-case delta `d_i = candidate_i − baseline_i`
and analyze the deltas directly. Pairing cancels case-to-case difficulty
variance and is materially more powerful than an unpaired normal band.

The canonical interval is the **paired percentile bootstrap** on the mean delta:
`paired_bootstrap_ci(deltas, alpha, n_resamples, seed) -> (lo, hi, mean)`.

- A candidate **within the paired noise band** yields a CI that **straddles 0**
  (`lo < 0 < hi`) → the gate **rejects**.
- A **clearly-better** candidate yields a CI **strictly above 0** (`lo > 0`) →
  the gate **adopts**.

The resample is seeded (`random.Random(seed)`), so the interval is reproducible.

## 5. Family-wise (Holm) correction

When several metrics are tested jointly, control the family-wise error rate with
the **Holm-Bonferroni step-down** procedure: `holm_correction(pvalues, alpha) ->
list[bool]`. Sort p-values ascending; compare the `i`-th smallest against
`α/(m−i)`; the first failure stops the step-down (it and all larger p-values are
not rejected). Returns reject decisions aligned with input order. A rich rubric
therefore cannot inflate the false-promotion rate past `α`.

## 6. Held-out tune-set vs gate-set split

**The Tuner must not gate on the set it searched.** Optimizing over an eval set
and then gating on that same set lets the optimizer overfit the eval — the
promoted candidate looks better than it is. Split the GoldenSet up front:

`tune_gate_split(cases, frac, seed) -> (tune, gate)`

The Tuner searches over `tune`; the final gate decision is computed only on the
held-out `gate` set the search never touched. The split is **disjoint**,
**covers every case exactly once**, and is **reproducible** for a given
`(cases, frac, seed)`.

## 7. Power / minimum-detectable-effect (GoldenSet sizing)

Size the GoldenSet so it can actually detect the effect you care about. For the
paired mean-delta test:

- `min_detectable_effect(n, std, alpha, power) -> float` — smallest true mean
  delta detectable with probability `power` at level `alpha` given `n` cases and
  per-case delta std `std`: `(z_{1−α/2} + z_power)·std/√n`.
- `required_n(mde, std, alpha, power) -> int` — the inverse: the smallest `n`
  giving `power` to detect `mde`.

Report the MDE next to every gate decision so a "no improvement" verdict can be
read as "no improvement **larger than X** was detectable at this `n`."

`k_from_alpha(alpha, two_sided) -> float` derives the noise-band multiplier `k`
from a stated `α` as the standard-normal quantile (`Φ^-1(1−α/2)` two-sided),
rather than leaving `k` a magic constant. At the `α→1`/`k=0`, `std=0` limit the
band collapses and the variance-aware gate reproduces today's zero-tolerance
`is_regression` byte-for-byte (F-3's compatibility requirement).

## 8. Anytime-valid sequential bound

`anytime_valid_bound(mean, std, n, alpha, rho) -> (lo, hi)` returns a confidence
sequence (normal-mixture / Robbins sub-Gaussian boundary) that is valid at every
`n` simultaneously. It is deliberately **wider** than the fixed-`n` CI — that
extra width is the price of being allowed to peek and stop early without
inflating the error rate.

## 9. Winner's-curse correction

Selecting the **argmax** over many candidates inflates the winner's measured
score (optimism bias). Before storing a promoted score as the new baseline —
which the next round must beat — **re-estimate it on a fresh, independent
sample** and shrink the inflated selection score toward that estimate:

`winners_curse_shrink(argmax_score, fresh_sample_score, weight) -> float`

`weight=1.0` (default) returns the fresh unbiased estimate; `weight=0.0` keeps
the inflated score; intermediate values interpolate. This prevents the gate bar
from **ratcheting up on noise** round after round.

---

## Conformance checklist (every statistical consumer)

A gate/consumer conforms to F-8 iff it:

1. States its estimand and designates a primary metric (§1–2).
2. Uses a **paired** test over GoldenSet deltas (§4), not an unpaired band.
3. Applies Holm correction **or** the primary+guardrail design (§2, §5).
4. Pre-registers `n` **or** uses the anytime-valid bound when it peeks (§3, §8).
5. Gates on a **held-out** set distinct from any set the Tuner searched (§6).
6. Reports the MDE for its `n` (§7) and derives `k` from `α`, not a constant.
7. **Shrinks a promoted argmax** on a fresh sample before storing it (§9).
8. Is **deterministic**: pure arithmetic over recorded scores; any bootstrap is
   explicitly seeded.

See the API and signatures in
[`crawfish.experiment`](../../packages/crawfish/src/crawfish/experiment.py) and
the changelog entry `docs/_changelog/F-8.md`.
