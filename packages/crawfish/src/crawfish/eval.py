"""Eval data lifecycle — cases, labeling, golden sets, LLM-judge.

The other half of the quality loop: the scoring *types*
(Metric/Rubric/Benchmark) live elsewhere; this ships the eval *data* lifecycle that lets the
"metrics correlate with quality" bet be validated. Capture real runs as reusable
eval cases, attach human labels, curate versioned golden sets, grade with an
LLM-as-judge (complementing coded Metrics), and gate a new Definition version
against a stored regression baseline.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from pydantic import BaseModel, Field

from crawfish.core.context import RunContext
from crawfish.core.ids import new_id
from crawfish.core.types import JSONValue
from crawfish.definition.types import Definition
from crawfish.emission import (
    CorrectionType,
    Provenance,
    read_corrections,
)
from crawfish.experiment import holm_correction, paired_bootstrap_ci
from crawfish.metrics import Rubric, is_regression
from crawfish.output import Output
from crawfish.runtime.base import AgentRuntime
from crawfish.runtime.team import run_team
from crawfish.store.base import Store
from crawfish.validation import canonicalize

__all__ = [
    "EvalCase",
    "GoldenSet",
    "LLMJudge",
    "capture_case",
    "grade_output",
    "save_baseline",
    "load_baseline",
    "gate_against_baseline",
    "upconvert_case",
    "migrate_golden_set",
    # -- the gate algebra (F-3 / CRA-196) --
    "VerifierNotGated",
    "MetricVerdict",
    "GateDecision",
    "paired_gate",
    "precision_gate",
]


class EvalCase(BaseModel):
    """A captured run made reusable: its inputs, the produced output, and an
    optional human label (expected output / judgment)."""

    id: str = Field(default_factory=new_id)
    inputs: dict[str, JSONValue] = Field(default_factory=dict)
    output: JSONValue = None
    produced_by: str | None = None
    transcript: list[JSONValue] = Field(default_factory=list)
    label: JSONValue = None  # human judgment / expected output
    metadata: dict[str, JSONValue] = Field(default_factory=dict)


def capture_case(
    *,
    inputs: dict[str, JSONValue],
    output: Output[JSONValue],
    transcript: list[JSONValue] | None = None,
    label: JSONValue = None,
) -> EvalCase:
    """Capture a real run (inputs + output [+ transcript]) as an eval case."""
    return EvalCase(
        inputs=dict(inputs),
        output=output.value,
        produced_by=output.produced_by,
        transcript=list(transcript or []),
        label=label,
    )


class GoldenSet:
    """A named, versioned set of labeled cases, persisted through the ``Store``."""

    def __init__(
        self, store: Store, name: str, *, org_id: str = "local", version: str = "0.1"
    ) -> None:
        self._store = store
        self.name = name
        self.version = version
        self._org = org_id

    @property
    def _kind(self) -> str:
        return f"golden:{self.name}@{self.version}"

    @classmethod
    def from_corrections(
        cls,
        store: Store,
        name: str = "corrections",
        *,
        org_id: str = "local",
        version: str = "0.1",
        kinds: Sequence[CorrectionType | str] = (
            CorrectionType.HUMAN_REVERT,
            CorrectionType.CI_FAILURE,
            CorrectionType.REVIEW_REJECT,
        ),
    ) -> GoldenSet:
        """Build a :class:`GoldenSet` by mining ``correction`` emissions (F-4).

        Sources the corrections corpus from the Store ledger (the records written by
        :func:`crawfish.emission.emit_correction`) for ``org_id``, filtered to the
        requested correction ``kinds`` (sub-categories
        ``human_revert``/``ci_failure``/``review_reject``), and turns each into an
        :class:`EvalCase` (inputs + the corrected ``expected`` output as the label).

        **SECURITY — provenance/taint gate (Gap S4, corpus poisoning).** Corrections
        feed guards/verifiers as ground truth, so this gate decides WHO may enter the
        set. A correction is admitted **only if** ``provenance == TRUSTED`` **AND**
        ``tainted is False``. Any correction that is ``UNTRUSTED`` (authored from
        fluid/untrusted session data) **or** carries the fluid taint marker is
        *quarantined*: it stays in the ledger for audit but never enters the
        GoldenSet as trusted ground truth. This is why the gate is an AND of both
        signals — a fluid-derived value cannot silently become a guard's ground truth
        even if mislabelled ``TRUSTED``.

        Org isolation: only ``org_id``'s correction records are read, and the built
        GoldenSet is persisted (each admitted case written back) under the same
        ``org_id``. Deterministic given a fixed ledger (no clock, no model call): the
        same ledger always yields the same set of cases (case ids are the emission
        ids). Returns the curated :class:`GoldenSet`.
        """
        gs = cls(store, name, org_id=org_id, version=version)
        wanted = tuple(k if isinstance(k, CorrectionType) else CorrectionType(k) for k in kinds)
        for em in read_corrections(store, org_id=org_id, kinds=wanted):
            # -- provenance/taint gate (Security S4): admit trusted ground truth only.
            provenance = em.attrs.get("provenance")
            if provenance != Provenance.TRUSTED.value or em.tainted:
                continue  # quarantine: untrusted or fluid-tainted correction
            raw_inputs = em.attrs.get("inputs")
            case = EvalCase(
                id=em.id,
                inputs=dict(raw_inputs) if isinstance(raw_inputs, dict) else {},
                output=em.attrs.get("produced"),
                label=em.attrs.get("expected"),
                metadata={
                    "source": "correction",
                    "correction_type": em.attrs.get("correction_type"),
                    "provenance": provenance,
                    "run_id": em.run_id,
                },
            )
            gs.add(case)
        return gs

    def add(self, case: EvalCase) -> None:
        self._store.put_record(self._kind, case.id, case.model_dump(mode="json"), org_id=self._org)

    def label(self, case_id: str, label: JSONValue) -> None:
        rec = self._store.get_record(self._kind, case_id, org_id=self._org)
        if rec is None:
            raise KeyError(f"no case {case_id!r} in golden set {self.name!r}")
        rec["label"] = label
        self._store.put_record(self._kind, case_id, rec, org_id=self._org)

    def get(self, case_id: str) -> EvalCase | None:
        rec = self._store.get_record(self._kind, case_id, org_id=self._org)
        return None if rec is None else EvalCase.model_validate(upconvert_case(rec))

    def cases(self) -> list[EvalCase]:
        return [
            EvalCase.model_validate(upconvert_case(r))
            for r in self._store.list_records(self._kind, org_id=self._org)
        ]

    def migrate(self) -> int:
        """Rewrite every stored case through :func:`upconvert_case`, persisting the
        typed form. Returns the count of cases whose ``output``/``label`` changed.

        Idempotent: a second call is a no-op (already-typed cases up-convert to
        themselves). Use this to bulk-lift a golden set captured in the string era to
        typed values in place; the lazy read path keeps callers correct meanwhile.
        """
        changed = 0
        for raw in self._store.list_records(self._kind, org_id=self._org):
            lifted = upconvert_case(raw)
            if lifted != raw:
                case = EvalCase.model_validate(lifted)
                self._store.put_record(
                    self._kind, case.id, case.model_dump(mode="json"), org_id=self._org
                )
                changed += 1
        return changed


# -- golden-set string→typed migration (CRA-172 handoff) ---------------------
def _lift_string(value: JSONValue) -> JSONValue:
    """Up-convert a string that holds a single JSON document to its typed value.

    A plain string that is NOT a self-contained JSON object/array (e.g. free text, or
    a model that emitted two objects) is left untouched — we never guess. Records are
    canonicalised so the lifted form is reproducible under record/replay.
    """
    if not isinstance(value, str):
        return canonicalize(value)
    stripped = value.strip()
    if not stripped or stripped[0] not in "{[":
        return value
    try:
        decoded, end = json.JSONDecoder().raw_decode(stripped)
    except (ValueError, TypeError):
        return value
    if stripped[end:].strip():
        return value  # trailing junk / second object — ambiguous, leave as string
    return canonicalize(decoded)


def upconvert_case(rec: dict[str, JSONValue]) -> dict[str, JSONValue]:
    """Up-convert a stored EvalCase row from the string era to typed values.

    Captured golden sets stored before CRA-172 hold ``output``/``label`` as JSON-encoded
    *strings*; metrics now read TYPED ``Output.value``. This lifts those fields in place
    (pure + deterministic). Already-typed rows pass through unchanged, so it is safe to
    apply on every read. This is the eval analogue of CRA-191's ``RECORD_UPCONVERTERS``:
    because golden-set ``kind`` values are dynamic (``golden:NAME@VERSION``), the lazy
    read path is applied in :meth:`GoldenSet.get`/:meth:`GoldenSet.cases` rather than via
    the static converter table.
    """
    out = dict(rec)
    if "output" in out:
        out["output"] = _lift_string(out["output"])
    if "label" in out:
        out["label"] = _lift_string(out["label"])
    return out


def migrate_golden_set(
    store: Store, name: str, *, version: str = "0.1", org_id: str = "local"
) -> int:
    """Bulk-migrate a named/versioned golden set's cases to typed values in place.

    Convenience wrapper over :meth:`GoldenSet.migrate`. Returns the number of cases
    rewritten.
    """
    return GoldenSet(store, name, org_id=org_id, version=version).migrate()


_SCORE_RE = re.compile(r"-?\d+(?:\.\d+)?")


def _parse_score(text: str) -> float:
    """Extract a [0,1] score from a judge's free-text verdict (clamped)."""
    m = _SCORE_RE.search(text)
    if not m:
        return 0.0
    return max(0.0, min(1.0, float(m.group())))


class LLMJudge:
    """A Definition-backed grader: an agent scores an output against criteria.

    Complements coded ``Metric``s. Deterministic under a mock/replay runtime.
    """

    def __init__(
        self, definition: Definition, runtime: AgentRuntime, *, name: str = "llm_judge"
    ) -> None:
        self.definition = definition
        self.runtime = runtime
        self.name = name

    async def grade(
        self, output: Output[JSONValue], ctx: RunContext, *, criteria: str = "quality"
    ) -> float:
        # The output value is bound as fluid (untrusted) data for the judge.
        inputs: dict[str, JSONValue] = {"output": output.value, "criteria": criteria}
        result = await run_team(self.definition, inputs, ctx, self.runtime)
        return _parse_score(result.text)


async def grade_output(
    output: Output[JSONValue],
    ctx: RunContext,
    *,
    rubric: Rubric | None = None,
    judges: list[LLMJudge] | None = None,
) -> dict[str, float]:
    """Combine coded-metric scores and LLM-judge grades into one score dict."""
    scores: dict[str, float] = {}
    if rubric is not None:
        scores.update(rubric.score(output))
    for judge in judges or []:
        scores[judge.name] = await judge.grade(output, ctx)
    return scores


# -- regression baselines -----------------------------------------------------
def save_baseline(
    store: Store, name: str, scores: dict[str, float], *, org_id: str = "local"
) -> None:
    store.put_record("eval_baseline", name, dict(scores), org_id=org_id)


def load_baseline(store: Store, name: str, *, org_id: str = "local") -> dict[str, float] | None:
    rec = store.get_record("eval_baseline", name, org_id=org_id)
    return None if rec is None else {k: float(v) for k, v in rec.items()}


def gate_against_baseline(
    store: Store,
    name: str,
    candidate: dict[str, float],
    *,
    tolerance: float = 0.0,
    org_id: str = "local",
) -> bool:
    """True if ``candidate`` passes (no regression vs the stored baseline)."""
    baseline = load_baseline(store, name, org_id=org_id)
    if baseline is None:
        return True  # no baseline yet — nothing to regress against
    return not is_regression(baseline, candidate, tolerance=tolerance)


# ===========================================================================
# The gate algebra (F-3 / CRA-196)
# ---------------------------------------------------------------------------
# ONE owner reconciling the three gate notions. Each consumer picks one gate:
#
#   (a) relative-regression  — ``gate_against_baseline`` / ``is_regression``
#       (above; unchanged). A candidate passes iff no metric drops below
#       ``-tolerance``. This is the cheap, aggregate-score gate used by callers
#       that retain only mean scores and want today's zero-tolerance behaviour.
#       ``metrics.is_regression_variance_aware`` is its variance-aware sibling
#       for callers that retain a recorded per-metric ``std`` but not per-case
#       deltas; at ``std=0`` it reduces to (a) byte-for-byte.
#
#   (b) variance-aware paired test — ``paired_gate`` (below). The Tuner /
#       ``calibrate`` / promotion gate uses this: baseline and candidate are
#       scored on the SAME GoldenSet cases, so we analyse per-case deltas with a
#       paired bootstrap CI (``experiment.paired_bootstrap_ci``) and a family-
#       wise Holm correction (``experiment.holm_correction``) OR a primary +
#       pre-registered non-inferiority guardrail design. Adopt iff the primary's
#       CI is strictly above 0 and no guardrail breaches its margin.
#
#   (c) absolute-precision — ``precision_gate`` (below). Verifiers / guards
#       (``VerifierStop`` / consequential sinks) use this. It is NOT relative:
#       it measures decision precision ``TP / (TP + FP)`` against a labelled
#       decision GoldenSet and FAILS CLOSED — a never-benchmarked verifier (no
#       baseline) is REJECTED by raising ``VerifierNotGated`` (the CL-2 safety
#       inversion: admit only after measuring, never by default).
#
# All three consume the shared substrate ``crawfish.experiment`` (F-8); no stats
# are re-implemented here. See ``docs/architecture/experiment-design.md``.
# ===========================================================================


class VerifierNotGated(Exception):
    """A consequential verifier/guard was used without an absolute-precision gate.

    Raised by :func:`precision_gate` when the verifier has **no baseline** (never
    benchmarked) — the gate **fails closed**: an un-measured verifier is rejected,
    never admitted by default. This is the CL-2 safety inversion fix.
    """


@dataclass(frozen=True)
class MetricVerdict:
    """The paired-gate result for a single metric: its CI and adopt/guardrail status."""

    name: str
    lo: float
    hi: float
    mean: float
    is_primary: bool
    passed: bool


@dataclass(frozen=True)
class GateDecision:
    """The outcome of :func:`paired_gate` — promote-or-not plus per-metric detail."""

    promoted: bool
    primary: str
    verdicts: tuple[MetricVerdict, ...] = field(default_factory=tuple)
    reason: str = ""


def _per_case_deltas(
    baseline_scores: Mapping[str, Sequence[float]],
    candidate_scores: Mapping[str, Sequence[float]],
    metric: str,
) -> list[float]:
    """Per-case deltas ``candidate_i - baseline_i`` for ``metric`` (paired design)."""
    base = baseline_scores.get(metric)
    cand = candidate_scores.get(metric)
    if base is None or cand is None:
        raise KeyError(f"metric {metric!r} missing from baseline or candidate scores")
    if len(base) != len(cand):
        raise ValueError(
            f"paired gate requires equal-length per-case scores for {metric!r}: "
            f"baseline has {len(base)}, candidate has {len(cand)}"
        )
    return [float(c) - float(b) for b, c in zip(base, cand, strict=True)]


def _ci_pvalue(lo: float, hi: float, mean: float) -> float:
    """A conservative bootstrap p-value proxy for "mean delta <= 0".

    Maps the one-sided bootstrap CI to a p-value for Holm: a CI strictly above 0
    is significant (small p), a CI straddling/below 0 is not. We use the standard
    "smallest two-sided level at which the CI excludes 0" reading: if the
    ``1-alpha`` CI (here built at the gate's own ``alpha``) has ``lo > 0`` the
    effect clears that ``alpha``; otherwise it does not. We return ``0.0`` when
    ``lo > 0`` (clears any threshold at this resolution) and ``1.0`` otherwise,
    deferring the family-wise control entirely to Holm over these decisions.
    """
    return 0.0 if lo > 0.0 else 1.0


def paired_gate(
    baseline_scores: Mapping[str, Sequence[float]],
    candidate_scores: Mapping[str, Sequence[float]],
    *,
    primary: str,
    alpha: float = 0.05,
    guardrails: Mapping[str, float] | None = None,
    n_resamples: int = 2000,
    seed: int = 0,
) -> GateDecision:
    """Variance-aware paired promotion gate (gate **b**) — F-3 / F-8 conformant.

    ``baseline_scores[m]`` / ``candidate_scores[m]`` are the **per-case** score
    vectors for metric ``m`` over the *same* GoldenSet cases (case ``i`` is the
    ``i``-th element of both). The gate analyses per-case deltas with a paired
    percentile bootstrap (:func:`crawfish.experiment.paired_bootstrap_ci`).

    Two designs (experiment-design.md §2), selected by ``guardrails``:

    * **Primary + guardrails** (``guardrails`` given): adopt iff ``primary``'s CI
      is strictly above 0 (``lo > 0``) **and** every guardrail metric's mean
      per-case delta does not drop below ``-margin`` (non-inferiority). Only the
      primary is an improvement target.
    * **Family-wise Holm** (``guardrails is None``): test every metric jointly,
      apply Holm (:func:`crawfish.experiment.holm_correction`) across them so the
      family-wise error stays at ``alpha``; promote iff the corrected decision for
      ``primary`` is "reject the null" (a real improvement).

    A candidate within the paired noise band (CI straddles 0) is rejected; a
    clearly-better candidate (CI strictly above 0) is promoted. Deterministic: the
    bootstrap is seeded.
    """
    if primary not in candidate_scores or primary not in baseline_scores:
        raise KeyError(f"primary metric {primary!r} missing from scores")

    if guardrails is not None:
        # Design 1: one primary improvement target + non-inferiority guardrails.
        deltas = _per_case_deltas(baseline_scores, candidate_scores, primary)
        lo, hi, mean = paired_bootstrap_ci(deltas, alpha, n_resamples, seed)
        primary_ok = lo > 0.0
        verdicts = [MetricVerdict(primary, lo, hi, mean, True, primary_ok)]
        breached: list[str] = []
        for name, margin in guardrails.items():
            g_deltas = _per_case_deltas(baseline_scores, candidate_scores, name)
            g_lo, g_hi, g_mean = paired_bootstrap_ci(g_deltas, alpha, n_resamples, seed)
            # Non-inferiority: the *lower* CI bound must clear the margin.
            ok = g_lo >= -abs(margin)
            if not ok:
                breached.append(name)
            verdicts.append(MetricVerdict(name, g_lo, g_hi, g_mean, False, ok))
        promoted = primary_ok and not breached
        if not primary_ok:
            reason = f"primary {primary!r} CI straddles/below 0 (lo={lo:.4g})"
        elif breached:
            reason = f"guardrail(s) breached: {', '.join(sorted(breached))}"
        else:
            reason = f"primary {primary!r} CI strictly above 0 (lo={lo:.4g})"
        return GateDecision(promoted, primary, tuple(verdicts), reason)

    # Design 2: family-wise Holm correction across the whole rubric.
    names = sorted(set(baseline_scores) & set(candidate_scores))
    if primary not in names:
        raise KeyError(f"primary metric {primary!r} not in shared metric set")
    cis: dict[str, tuple[float, float, float]] = {}
    pvalues: list[float] = []
    for name in names:
        deltas = _per_case_deltas(baseline_scores, candidate_scores, name)
        lo, hi, mean = paired_bootstrap_ci(deltas, alpha, n_resamples, seed)
        cis[name] = (lo, hi, mean)
        pvalues.append(_ci_pvalue(lo, hi, mean))
    rejects = holm_correction(pvalues, alpha)
    decisions = dict(zip(names, rejects, strict=True))
    verdicts = [MetricVerdict(name, *cis[name], name == primary, decisions[name]) for name in names]
    promoted = decisions[primary]
    reason = (
        f"primary {primary!r} significant after Holm (m={len(names)})"
        if promoted
        else f"primary {primary!r} not significant after Holm (m={len(names)})"
    )
    return GateDecision(promoted, primary, tuple(verdicts), reason)


def precision_gate(
    decisions: Sequence[bool],
    labels: Sequence[bool],
    *,
    min_precision: float,
    baseline_exists: bool,
) -> float:
    """Absolute-precision gate (gate **c**) for verifiers/guards — FAILS CLOSED.

    A verifier emits a positive ``decision`` (e.g. "stop / admit / this is correct")
    per labelled case; ``labels[i]`` is the ground-truth positive. Precision is
    ``TP / (TP + FP)`` against the decision GoldenSet. The candidate is admitted
    iff ``precision >= min_precision`` **and** a baseline exists.

    **Fails closed (CL-2 safety inversion).** A never-benchmarked verifier
    (``baseline_exists is False``) is *rejected by construction*: this raises
    :class:`VerifierNotGated` rather than returning. An un-measured verifier is
    never admitted by default — admission requires having measured it. Likewise a
    verifier whose measured precision is below ``min_precision`` raises.

    Returns the measured precision on success (so the caller can record it). Note
    this is an **absolute** decision-quality gate, unlike the relative gates (a)/(b).
    """
    if not baseline_exists:
        raise VerifierNotGated(
            "verifier has no baseline — never benchmarked; the precision gate "
            "fails closed (admit only after measuring against the decision GoldenSet)"
        )
    if len(decisions) != len(labels):
        raise ValueError(
            f"precision_gate needs aligned decisions/labels: "
            f"{len(decisions)} decisions vs {len(labels)} labels"
        )
    if not 0.0 <= min_precision <= 1.0:
        raise ValueError(f"min_precision must be in [0, 1], got {min_precision!r}")
    tp = sum(1 for d, y in zip(decisions, labels, strict=True) if d and y)
    fp = sum(1 for d, y in zip(decisions, labels, strict=True) if d and not y)
    predicted_positive = tp + fp
    if predicted_positive == 0:
        # No positive predictions ⇒ precision undefined ⇒ fail closed.
        raise VerifierNotGated(
            "verifier made no positive decisions on the GoldenSet — precision is "
            "undefined; the gate fails closed"
        )
    precision = tp / predicted_positive
    if precision < min_precision:
        raise VerifierNotGated(
            f"verifier precision {precision:.4g} below required {min_precision:.4g} — rejected"
        )
    return precision
