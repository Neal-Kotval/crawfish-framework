"""Metrics, Rubrics & Benchmarks — the improvement loop.

Make agent quality *measurable and comparable* across Definition versions. A
:class:`Metric` scores a single :class:`~crawfish.output.Output` to a float; a
:class:`Rubric` bundles metrics into a named score vector; a :class:`Benchmark`
runs a rubric over a fixed task set and aggregates (mean per metric) into
*comparable, ordered* scores. The payoff is the improvement loop: run two
Definition versions over the same tasks, :func:`compare` the score vectors, and
:func:`is_regression` flags a candidate that got worse.

Kept deterministic with :class:`~crawfish.runtime.mock.MockRuntime`: no model
call, so iterating on metrics never burns budget and scores never drift.
"""

from __future__ import annotations

import hashlib
import json
import random
import re
import statistics
from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from crawfish.batch import Task
from crawfish.core.context import BudgetExceeded, RunContext
from crawfish.core.types import JSONValue, Parameter
from crawfish.definition.types import Definition
from crawfish.escalate import abstention_threshold, extract_confidence
from crawfish.experiment import k_from_alpha
from crawfish.output import Output
from crawfish.run import Run
from crawfish.runtime.base import AgentRuntime, DeterminismTier, RunRequest, RunResult
from crawfish.runtime.prompt import split_inputs
from crawfish.validation import canonicalize, structural_diff, validate_output

if TYPE_CHECKING:
    from crawfish.eval import EvalCase, GoldenSet

__all__ = [
    "Metric",
    "Rubric",
    "Benchmark",
    "OutputNumber",
    "FieldPresent",
    "IsNonempty",
    "ConfidenceThreshold",
    "FieldExactMatch",
    "SetOverlap",
    "NumericTolerance",
    "SchemaConformance",
    "StructuralMatch",
    "output_number",
    "field_present",
    "is_nonempty",
    "confidence_threshold",
    "field_exact_match",
    "set_overlap",
    "numeric_tolerance",
    "schema_conformance",
    "structural_match",
    "compare",
    "is_regression",
    "noise_band",
    "is_regression_variance_aware",
    # -- calibration (CRA-211 / AL-T4 / TS-2) --
    "CalibrationError",
    "ReliabilityBin",
    "CalibrationReport",
    "calibrate",
]

_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")


def _typed_value(output: Output[JSONValue]) -> JSONValue:
    """The TYPED, canonicalised Output value.

    Post-CRA-172, a Definition with a declared (RECORD/LIST) schema carries a real
    ``dict``/``list`` in ``Output.value``; semantic/structural metrics read that
    directly. A plain string (no-schema back-compat) is decoded **only if** it is a
    single self-contained JSON document — guarding the CRA-172 follow-up so multiple
    emitted objects never silently score the wrong one. Records are canonicalised so
    unordered keys score reproducibly under record/replay.
    """
    value = output.value
    if isinstance(value, str):
        decoded = _decode_single_json(value)
        if decoded is not None:
            value = decoded
    return canonicalize(value)


def _decode_single_json(text: str) -> JSONValue | None:
    """Decode ``text`` iff it is exactly ONE JSON document (no trailing junk).

    Guards the multi-JSON-object hazard (CRA-172 follow-up): ``raw_decode`` parses the
    first value, then we require the remainder to be whitespace. ``{"a":1}{"b":2}`` and
    ``{"a":1}\n{"b":2}`` therefore return ``None`` rather than scoring just ``{"a":1}``.
    """
    stripped = text.strip()
    if not stripped or stripped[0] not in "{[":
        return None
    try:
        decoded, end = json.JSONDecoder().raw_decode(stripped)
    except (ValueError, TypeError):
        return None
    if stripped[end:].strip():
        return None  # a second object follows — ambiguous, refuse to guess
    return decoded


def _as_mapping(value: object) -> dict[str, JSONValue] | None:
    """View an Output value as a string->value mapping for field metrics.

    Post-CRA-172 a Definition with a typed (RECORD) output schema already carries a
    ``dict`` in ``Output.value`` — that path is read directly with no decoding. The
    string-decode fallback survives only for the **back-compat** case: a Definition with
    *no* declared outputs keeps a plain-string ``Output.value`` (``RunResult.text``);
    when that string JSON-encodes a *single* object we decode it so field metrics still
    apply. Multiple JSON objects are refused (see :func:`_decode_single_json`).
    """
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        decoded = _decode_single_json(value)
        if isinstance(decoded, dict):
            return decoded
    return None


def _field(value: JSONValue, field: str | None) -> JSONValue:
    """Resolve ``field`` (dotted path) within a typed value; ``None`` if absent."""
    if field is None:
        return value
    cur: JSONValue = value
    for part in field.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur


def _as_members(value: JSONValue) -> set[str]:
    """Normalise a value to a set of JSON-string members (order-free, hashable)."""
    if isinstance(value, (list, tuple, set, frozenset)):
        items: list[JSONValue] = list(value)
    elif value is None:
        items = []
    else:
        items = [value]
    return {json.dumps(canonicalize(i), sort_keys=True) for i in items}


# -- the metric protocol ----------------------------------------------------
class Metric(ABC):
    """A single scalar quality signal over one Output.

    ``name`` keys the metric in a :class:`Rubric` score vector; ``evaluate``
    returns a float (convention: higher is better; presence/format metrics use
    ``1.0``/``0.0`` as a pass/fail).
    """

    name: str

    @abstractmethod
    def evaluate(self, output: Output[JSONValue]) -> float:
        """Score ``output`` to a float."""


# -- starter metric library -------------------------------------------------
class OutputNumber(Metric):
    """Extract a numeric from the Output value.

    If the value is itself a number it is returned directly; a mapping is probed
    by ``field``; otherwise the first numeric token in the string form is used.
    ``default`` is returned when nothing numeric is found.
    """

    def __init__(self, *, field: str | None = None, default: float = 0.0, name: str | None = None):
        self.field = field
        self.default = default
        self.name = name or ("output_number" if field is None else f"output_number[{field}]")

    def evaluate(self, output: Output[JSONValue]) -> float:
        value: object = output.value
        if self.field is not None:
            mapping = _as_mapping(value)
            value = mapping.get(self.field) if mapping is not None else None
        if isinstance(value, bool):
            return float(value)
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            match = _NUMBER_RE.search(value)
            if match is not None:
                return float(match.group())
        return self.default


class FieldPresent(Metric):
    """``1.0`` if ``field`` is present (and non-null) in the Output value, else ``0.0``."""

    def __init__(self, field: str, *, name: str | None = None):
        self.field = field
        self.name = name or f"field_present[{field}]"

    def evaluate(self, output: Output[JSONValue]) -> float:
        mapping = _as_mapping(output.value)
        if mapping is None:
            return 0.0
        return 1.0 if mapping.get(self.field) is not None else 0.0


class IsNonempty(Metric):
    """``1.0`` if the Output value is non-empty, else ``0.0``.

    Empty means: empty string/whitespace, empty collection, ``None``, or a
    mapping whose ``field`` is empty when ``field`` is given.
    """

    def __init__(self, *, field: str | None = None, name: str | None = None):
        self.field = field
        self.name = name or ("is_nonempty" if field is None else f"is_nonempty[{field}]")

    def evaluate(self, output: Output[JSONValue]) -> float:
        value: object = output.value
        if self.field is not None:
            mapping = _as_mapping(value)
            value = mapping.get(self.field) if mapping is not None else None
        if value is None:
            return 0.0
        if isinstance(value, str):
            return 1.0 if value.strip() else 0.0
        if isinstance(value, (list, dict, tuple, set)):
            return 1.0 if len(value) else 0.0
        return 1.0


class ConfidenceThreshold(Metric):
    """``1.0`` if ``field``'s numeric confidence is ``>= threshold``, else ``0.0``."""

    def __init__(self, field: str, threshold: float, *, name: str | None = None):
        self.field = field
        self.threshold = threshold
        self.name = name or f"confidence_threshold[{field}>={threshold}]"

    def evaluate(self, output: Output[JSONValue]) -> float:
        score = OutputNumber(field=self.field, default=float("-inf")).evaluate(output)
        return 1.0 if score >= self.threshold else 0.0


# -- structured-output & semantic metrics (read TYPED Output.value) ---------
class FieldExactMatch(Metric):
    """``1.0`` if ``field`` (dotted path) of the typed value equals ``expected``.

    Comparison is canonical: records are key-sorted and lists keep order, so a
    ``{"a":1,"b":2}`` value matches an ``{"b":2,"a":1}`` expectation. ``field=None``
    compares the whole value.
    """

    def __init__(self, expected: JSONValue, *, field: str | None = None, name: str | None = None):
        self.expected = canonicalize(expected)
        self.field = field
        suffix = "" if field is None else f"[{field}]"
        self.name = name or f"field_exact_match{suffix}"

    def evaluate(self, output: Output[JSONValue]) -> float:
        actual = canonicalize(_field(_typed_value(output), self.field))
        return 1.0 if actual == self.expected else 0.0


class SetOverlap(Metric):
    """Order-free overlap of a list/set ``field`` against ``expected`` members.

    ``mode`` selects the score: ``"f1"`` (harmonic mean of precision/recall, the
    default) or ``"jaccard"`` (intersection / union). Members are compared by canonical
    JSON so nested records/order do not matter. Two empty sets score ``1.0``.
    """

    def __init__(
        self,
        expected: JSONValue,
        *,
        field: str | None = None,
        mode: str = "f1",
        name: str | None = None,
    ):
        if mode not in ("f1", "jaccard"):
            raise ValueError(f"mode must be 'f1' or 'jaccard', got {mode!r}")
        self.expected = _as_members(expected)
        self.field = field
        self.mode = mode
        suffix = "" if field is None else f"[{field}]"
        self.name = name or f"set_overlap.{mode}{suffix}"

    def evaluate(self, output: Output[JSONValue]) -> float:
        actual = _as_members(_field(_typed_value(output), self.field))
        inter = len(actual & self.expected)
        if not actual and not self.expected:
            return 1.0
        if self.mode == "jaccard":
            union = len(actual | self.expected)
            return inter / union if union else 1.0
        # F1: precision = inter/|actual|, recall = inter/|expected|
        if inter == 0:
            return 0.0
        precision = inter / len(actual)
        recall = inter / len(self.expected)
        return 2 * precision * recall / (precision + recall)


class NumericTolerance(Metric):
    """``1.0`` if a numeric ``field`` is within ``tol`` of ``expected``, else ``0.0``.

    ``relative=True`` makes ``tol`` a fraction of ``|expected|`` (with an absolute floor
    for ``expected == 0``). Non-numeric/absent values score ``0.0``.
    """

    def __init__(
        self,
        expected: float,
        *,
        field: str | None = None,
        tol: float = 1e-9,
        relative: bool = False,
        name: str | None = None,
    ):
        self.expected = float(expected)
        self.field = field
        self.tol = float(tol)
        self.relative = relative
        suffix = "" if field is None else f"[{field}]"
        self.name = name or f"numeric_tolerance{suffix}"

    def evaluate(self, output: Output[JSONValue]) -> float:
        raw = _field(_typed_value(output), self.field)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            return 0.0
        bound = self.tol
        if self.relative:
            bound = max(self.tol * abs(self.expected), self.tol)
        return 1.0 if abs(float(raw) - self.expected) <= bound else 0.0


class SchemaConformance(Metric):
    """Fraction in ``[0,1]`` of declared-schema checks the typed value passes.

    Re-validates the (string) Output against ``schema`` via
    :func:`~crawfish.validation.validate_output`; the score is ``1 - errors/checks``
    where ``checks`` is the number of declared *leaf* fields the schema resolves to
    (so a 2-field record missing one field scores ``0.5``, not ``0.0``). A clean parse
    with no errors is ``1.0``; an unparseable payload yields a single ``NOT_JSON`` error
    against ``checks`` and, for a one-field schema, scores ``0.0``.
    """

    def __init__(self, schema: list[Parameter], *, name: str | None = None):
        self.schema = list(schema)
        self.name = name or "schema_conformance"

    def evaluate(self, output: Output[JSONValue]) -> float:
        from crawfish.validation import ValidationFailure

        value = output.value
        text = value if isinstance(value, str) else json.dumps(value)
        _typed, errors = validate_output(text, self.schema)
        # An unparseable payload is a total failure, not "one error of N".
        if any(e.failure is ValidationFailure.NOT_JSON for e in errors):
            return 0.0
        checks = max(_schema_leaf_count(self.schema), 1)
        return max(0.0, 1.0 - len(errors) / checks)


def _schema_leaf_count(schema: list[Parameter]) -> int:
    """Number of declared leaf fields the ``schema`` resolves to (records recursed)."""
    from crawfish.typesystem.registry import TypeKind, default_registry

    def count(type_name: str, seen: frozenset[str]) -> int:
        if type_name in seen:
            return 1
        td = default_registry.resolve(type_name)
        if td.kind is TypeKind.RECORD and td.fields:
            return sum(count(ft, seen | {type_name}) for ft in td.fields.values())
        if td.kind is TypeKind.OPTIONAL and td.item is not None:
            return count(td.item, seen | {type_name})
        return 1

    return sum(count(p.type, frozenset()) for p in schema)


class StructuralMatch(Metric):
    """Semantic-diff score of the typed value against an ``expected`` value.

    Uses :func:`~crawfish.validation.structural_diff` (order-canonical for records).
    ``1.0`` when the diff is empty; otherwise ``1 - changes/total_paths`` so a value
    that differs in one of ten fields scores ``0.9``. A field ``path`` restricts the
    comparison to that subtree.
    """

    def __init__(self, expected: JSONValue, *, field: str | None = None, name: str | None = None):
        self.expected = canonicalize(expected)
        self.field = field
        suffix = "" if field is None else f"[{field}]"
        self.name = name or f"structural_match{suffix}"

    def evaluate(self, output: Output[JSONValue]) -> float:
        actual = _field(_typed_value(output), self.field)
        diff = structural_diff(self.expected, actual)
        if diff.equal:
            return 1.0
        changes = len(diff.added) + len(diff.removed) + len(diff.changed)
        total = _leaf_count(self.expected) + len(diff.added)
        if total <= 0:
            return 0.0
        return max(0.0, 1.0 - changes / total)


def _leaf_count(value: JSONValue) -> int:
    """Number of leaf paths in ``value`` (records/lists recursed; min 1)."""
    if isinstance(value, dict):
        return sum(_leaf_count(v) for v in value.values()) or 1
    if isinstance(value, (list, tuple)):
        return sum(_leaf_count(v) for v in value) or 1
    return 1


# -- factory aliases (Metric subclasses, exposed as ergonomic factories) -----
def field_exact_match(expected: JSONValue, *, field: str | None = None) -> FieldExactMatch:
    """Factory: a metric that checks a field equals ``expected`` (canonical compare)."""
    return FieldExactMatch(expected, field=field)


def set_overlap(expected: JSONValue, *, field: str | None = None, mode: str = "f1") -> SetOverlap:
    """Factory: an order-free set-overlap metric (F1 or Jaccard) over a list field."""
    return SetOverlap(expected, field=field, mode=mode)


def numeric_tolerance(
    expected: float, *, field: str | None = None, tol: float = 1e-9, relative: bool = False
) -> NumericTolerance:
    """Factory: a metric that checks a numeric field is within tolerance of ``expected``."""
    return NumericTolerance(expected, field=field, tol=tol, relative=relative)


def schema_conformance(schema: list[Parameter]) -> SchemaConformance:
    """Factory: a metric scoring how well the typed value conforms to ``schema``."""
    return SchemaConformance(schema)


def structural_match(expected: JSONValue, *, field: str | None = None) -> StructuralMatch:
    """Factory: a semantic-diff metric scoring the value against ``expected``."""
    return StructuralMatch(expected, field=field)


def output_number(*, field: str | None = None, default: float = 0.0) -> OutputNumber:
    """Factory: a metric that extracts a numeric from the Output value."""
    return OutputNumber(field=field, default=default)


def field_present(field: str) -> FieldPresent:
    """Factory: a metric that checks a field is present in the Output value."""
    return FieldPresent(field)


def is_nonempty(*, field: str | None = None) -> IsNonempty:
    """Factory: a metric that checks the Output value (or a field) is non-empty."""
    return IsNonempty(field=field)


def confidence_threshold(field: str, threshold: float) -> ConfidenceThreshold:
    """Factory: a metric that checks a field's confidence clears ``threshold``."""
    return ConfidenceThreshold(field, threshold)


# -- rubric -----------------------------------------------------------------
class Rubric:
    """A named collection of metrics scored together into one vector."""

    def __init__(self, metrics: Sequence[Metric], *, name: str = "rubric"):
        self.name = name
        self.metrics: list[Metric] = list(metrics)

    def score(self, output: Output[JSONValue]) -> dict[str, float]:
        """Score ``output`` with every metric -> ``{metric.name: float}``."""
        return {metric.name: metric.evaluate(output) for metric in self.metrics}


# -- benchmark --------------------------------------------------------------
def _task_inputs(task: Task) -> dict[str, JSONValue]:
    """Bind a Task's description as the conventional ``task`` fluid input."""
    return {"task": task.description, "task_id": task.id}


class Benchmark:
    """A rubric run over a fixed task set, aggregated to comparable scores.

    Each task drives one :class:`~crawfish.run.Run` of the Definition; the rubric
    scores each resulting Output; per-metric scores are aggregated (mean) into a
    single comparable vector. Deterministic under ``MockRuntime``.
    """

    def __init__(
        self,
        rubric: Rubric,
        tasks: Sequence[Task],
        *,
        name: str = "benchmark",
        inputs_for: Callable[[Task], dict[str, JSONValue]] | None = None,
    ):
        self.name = name
        self.rubric = rubric
        self.tasks: list[Task] = list(tasks)
        self.inputs_for = inputs_for or _task_inputs

    async def run(
        self,
        definition: Definition,
        ctx: RunContext,
        runtime: AgentRuntime,
    ) -> dict[str, float]:
        """Execute ``definition`` on every task, aggregate rubric scores (mean)."""
        totals: dict[str, float] = {metric.name: 0.0 for metric in self.rubric.metrics}
        count = 0
        for task in self.tasks:
            ctx.cancel_token.raise_if_cancelled()
            run = Run(definition, self.inputs_for(task), runtime=runtime)
            output = await run.execute(ctx, runtime)
            for name, value in self.rubric.score(output).items():
                totals[name] += value
            count += 1
        if count == 0:
            return {name: 0.0 for name in totals}
        return {name: total / count for name, total in totals.items()}


# -- the improvement-loop payoff --------------------------------------------
def compare(scores_a: dict[str, float], scores_b: dict[str, float]) -> dict[str, float]:
    """Per-metric deltas ``b - a`` (candidate minus baseline).

    Positive means the candidate improved on that metric; negative is a drop.
    Metrics absent from a side are treated as ``0.0`` so vectors need not align.
    """
    names = set(scores_a) | set(scores_b)
    return {name: scores_b.get(name, 0.0) - scores_a.get(name, 0.0) for name in names}


def is_regression(
    baseline: dict[str, float],
    candidate: dict[str, float],
    *,
    tolerance: float = 0.0,
) -> bool:
    """True if ``candidate`` is worse than ``baseline`` on any metric.

    A metric regresses when its delta drops below ``-tolerance`` (so a small
    ``tolerance`` absorbs noise). Higher-is-better is assumed for every metric.
    """
    for delta in compare(baseline, candidate).values():
        if delta < -tolerance:
            return True
    return False


def noise_band(std: dict[str, float], *, alpha: float = 0.05) -> dict[str, float]:
    """Per-metric noise-band half-width ``k * std`` from a stated significance ``alpha``.

    ``k`` is **derived** from ``alpha`` via :func:`crawfish.experiment.k_from_alpha`
    (the two-sided standard-normal quantile), never a free constant (F-3 / F-8).
    A metric with recorded ``std == 0`` (or absent) contributes a zero-width band,
    so at ``std=0`` the band vanishes and the gate collapses to ``is_regression``.
    """
    k = k_from_alpha(alpha)
    return {name: k * max(0.0, s) for name, s in std.items()}


def is_regression_variance_aware(
    baseline: dict[str, float],
    candidate: dict[str, float],
    *,
    std: dict[str, float] | None = None,
    alpha: float = 0.05,
    tolerance: float = 0.0,
) -> bool:
    """Variance-aware regression check: gate (b) reduced to recorded per-metric std.

    Identical to :func:`is_regression` except the per-metric noise tolerance is
    widened by that metric's ``k * std`` band (``k`` from ``alpha`` via
    :func:`crawfish.experiment.k_from_alpha`). A metric regresses only when its
    delta drops below ``-(tolerance + k * std_metric)`` — i.e. past the noise band.

    **Back-compat (F-3 hard requirement).** When every metric's ``std`` is ``0``
    (or ``std is None``) the band is zero-width, so this reduces **byte-for-byte**
    to ``is_regression(baseline, candidate, tolerance=tolerance)``. This is the
    arithmetic-only sibling of :func:`paired_gate` for callers that only retain
    aggregate scores + a recorded std (no per-case deltas).
    """
    band = noise_band(std or {}, alpha=alpha)
    for name, delta in compare(baseline, candidate).items():
        if delta < -(tolerance + band.get(name, 0.0)):
            return True
    return False


# ===========================================================================
# cw.calibrate — variance / calibration / abstention (CRA-211 / AL-T4 / TS-2)
# ---------------------------------------------------------------------------
# Measure a Definition's run-to-run *noise* and the honesty of its *confidence*,
# the two signals every tameness/promotion component keys off:
#
#   * run-to-run VARIANCE — per-metric ``rubric_std`` (the noise band Refine,
#     the no-progress detector, and the promotion gate read) and
#     ``output_variance`` (structural disagreement across re-runs).
#   * CONFIDENCE CALIBRATION — Brier score (primary, unbiased, no binning) +
#     ECE (a binned *diagnostic* with a bootstrap CI). Gating is forbidden when
#     a calibration metric's CI is wider than the gate margin (F-8): a noisy
#     point estimate cannot reintroduce noise-promotion.
#   * ABSTENTION — the confidence below which acting is unsafe, derived from the
#     reliability curve (escalate.abstention_threshold), not a guessed constant.
#
# Determinism: the ONLY stochastic point is the model call, varied through a
# per-run ``decode_seed`` derived purely from one recorded base seed. Seed
# derivation, aggregation, std/Brier/ECE are pure arithmetic. A
# RecordReplayRuntime is refused (replay would zero variance) — the one runtime
# calibrate legitimately drives non-replayed.
# ===========================================================================


class CalibrationError(RuntimeError):
    """Calibrate was asked to measure variance against a runtime that cannot show it.

    Raised when the supplied runtime is a ``RecordReplayRuntime``: replay returns a
    recorded result with zero run-to-run variance, so a "calibration" over it would
    silently report ``output_variance == 0`` and a fabricated zero noise band. Calibrate
    refuses loudly rather than emit a falsely-confident report.
    """


class ReliabilityBin(BaseModel):
    """One bin of the confidence→accuracy reliability curve (frozen).

    ``confidence`` is the mean self-reported confidence of the bin's members; ``accuracy``
    is the observed correctness rate; ``count`` is the population. The bins are equal-mass
    (adaptive), so a skewed confidence distribution still yields well-populated bins.
    """

    model_config = {"frozen": True}

    confidence: float
    accuracy: float
    count: int


class CalibrationReport(BaseModel):
    """The frozen, ``org_id``-tagged measurement of a Definition's noise + calibration.

    Consumed by the variance-aware promotion gate (AL-T5) and the cost-regularized
    objective (AL-T3): both read ``rubric_std`` (the per-metric noise band) and the
    calibration fields. Field contract (stable for those consumers):

    * ``rubric_mean`` / ``rubric_std`` — per-metric mean and *population* std across the
      ``runs × len(golden)`` scored outputs (``std`` is the noise band a ``*_std`` gate
      keys off; ``0.0`` for a single observation or a fully deterministic runtime).
    * ``output_variance`` — mean fraction of structurally-differing fields across the
      re-runs of each case (via :func:`~crawfish.validation.structural_diff`); ``0.0`` iff
      every re-run of every case agreed byte-for-byte (the deterministic-runtime case).
    * ``brier`` — primary calibration metric (mean squared error of confidence vs.
      correctness); ``None`` when no case carried a label (correctness undefined).
    * ``ece`` / ``ece_ci`` — Expected Calibration Error diagnostic and its
      ``(lo, hi)`` bootstrap CI; both ``None`` without labels. ``ece`` is in ``[0,1]``.
    * ``reliability`` — the equal-mass reliability curve the abstention threshold is read
      off (empty without labels).
    * ``abstention_threshold`` — the confidence below which acting is unsafe (derived from
      ``reliability``; ``1.0`` — abstain on everything — without labels or evidence).
    * ``abstention_rate`` — the share of scored outputs whose confidence fell below
      ``abstention_threshold`` (what an ``abstain_below`` policy would abstain on).
    * ``determinism_tier`` — the runtime's advertised determinism capability (F-5); when it
      is not ``honors-seed`` a non-zero ``infra_variance_floor`` is attributed to infra so
      model stochasticity is not conflated with backend nondeterminism.
    * ``base_seed`` / ``runs`` / ``cases`` — the reproducibility coordinates: the same
      ``(base_seed, runs)`` over the same golden yields an identical per-run seed schedule.
    * ``partial`` — ``True`` when a budget/cancel ceiling cut the measurement short (the
      Tuner's ceiling-returns-base analogue); the report still reflects what was measured.
    """

    model_config = {"frozen": True}

    org_id: str
    definition_id: str
    definition_version: str
    content_sha: str
    base_seed: int
    runs: int
    cases: int
    determinism_tier: DeterminismTier
    rubric_mean: dict[str, float] = Field(default_factory=dict)
    rubric_std: dict[str, float] = Field(default_factory=dict)
    output_variance: float = 0.0
    infra_variance_floor: float = 0.0
    brier: float | None = None
    ece: float | None = None
    ece_ci: tuple[float, float] | None = None
    reliability: tuple[ReliabilityBin, ...] = ()
    abstention_threshold: float = 1.0
    abstention_rate: float = 0.0
    partial: bool = False

    def gate_safe(self, margin: float) -> bool:
        """True if a calibration gate may rely on ``ece`` at this ``margin`` (F-8).

        Forbids gating when the ECE diagnostic's CI is wider than the gate margin: a
        high-variance point estimate must not reintroduce noise-promotion. With no ECE CI
        (no labels) there is nothing to gate on, so this returns ``False`` (fail safe).
        """
        if self.ece_ci is None:
            return False
        lo, hi = self.ece_ci
        return (hi - lo) <= abs(margin)


# -- seed derivation (pure, static, reproducible) ---------------------------
def _run_seed(base_seed: int, case_id: str, run_index: int) -> int:
    """Per-run decode seed, derived purely from the base seed (the F-1 / FewShot discipline).

    ``random.Random(f"{base_seed}:{case_id}:{run_index}")`` seeds a local RNG from a stable
    string and draws one 63-bit int. Same ``(base_seed, case_id, run_index)`` ⇒ identical
    seed across processes; distinct runs of a case get distinct seeds, so a seed-honouring
    runtime varies its decode per run (the only stochastic point). Never fluid-derived: the
    base seed is recorded, the case id is golden data, the index is positional.
    """
    rng = random.Random(f"{base_seed}:{case_id}:{run_index}")
    return rng.getrandbits(63)


def _golden_cases(golden: GoldenSet | Sequence[EvalCase]) -> list[EvalCase]:
    """Materialise the golden cases (a ``GoldenSet`` or an explicit case sequence)."""
    cases = golden.cases() if hasattr(golden, "cases") else list(golden)
    # Sort by id so the per-run seed schedule is order-free and reproducible.
    return sorted(cases, key=lambda c: c.id)


def _output_from_result(
    definition: Definition, inputs: dict[str, JSONValue], result: RunResult
) -> Output[JSONValue]:
    """Build the typed ``Output`` from a raw ``RunResult`` (the ``Run`` taint discipline).

    Mirrors :meth:`crawfish.run.Run.execute`: validate the text against the Definition's
    declared outputs to get the typed value, and taint it when any input was fluid. No
    persistence — calibrate measures, it doesn't write Outputs.
    """
    typed, _errors = validate_output(result.text, list(definition.outputs))
    value: JSONValue = typed if definition.outputs else result.text
    _static, fluid = split_inputs(definition, inputs)
    return Output(
        output_schema=list(definition.outputs),
        value=value,
        produced_by=definition.id,
        tainted=bool(fluid),
    )


# -- calibration arithmetic (pure) ------------------------------------------
def _structural_disagreement(values: Sequence[JSONValue]) -> float:
    """Mean fraction of structurally-differing fields across re-runs of one case.

    Pairs each re-run against the first and averages the change fraction
    (``changes / leaves``) via :func:`~crawfish.validation.structural_diff`. ``0.0`` when
    every re-run agreed (the deterministic-runtime invariant); ``> 0`` under genuine
    run-to-run drift. A single observation has nothing to disagree with → ``0.0``.
    """
    if len(values) < 2:
        return 0.0
    reference = canonicalize(values[0])
    fractions: list[float] = []
    for other in values[1:]:
        diff = structural_diff(reference, canonicalize(other))
        if diff.equal:
            fractions.append(0.0)
            continue
        changes = len(diff.added) + len(diff.removed) + len(diff.changed)
        leaves = max(_leaf_count(reference) + len(diff.added), 1)
        fractions.append(min(1.0, changes / leaves))
    return statistics.fmean(fractions) if fractions else 0.0


def _correct(output: Output[JSONValue], label: JSONValue) -> float:
    """Binary ground-truth correctness: ``1.0`` iff the typed value matches ``label``.

    Calibration scores a *confidence* against whether the run was right or wrong, so
    correctness is an **indicator** (exact structural match), not partial credit — a
    fractional correctness would smear the reliability curve and bias Brier/ECE.
    """
    diff = structural_diff(canonicalize(label), _typed_value(output))
    return 1.0 if diff.equal else 0.0


def _brier(confidences: Sequence[float], correct: Sequence[float]) -> float | None:
    """Brier score: mean squared error of confidence vs. correctness (lower is better).

    The primary calibration metric (unbiased, binning-free). ``None`` when there is no
    labelled observation to score against.
    """
    pairs = [(c, y) for c, y in zip(confidences, correct, strict=True) if c is not None]
    if not pairs:
        return None
    return statistics.fmean((c - y) ** 2 for c, y in pairs)


def _equal_mass_bins(
    confidences: Sequence[float], correct: Sequence[float], *, n_bins: int = 10
) -> list[ReliabilityBin]:
    """Equal-mass (adaptive) reliability bins: each holds ~the same number of points.

    Equal-mass beats equal-width when confidences cluster: every bin stays populated, so
    the curve (and the ECE read off it) is not dominated by a single fat bin. Points are
    sorted by confidence and split into ``n_bins`` near-equal contiguous chunks.
    """
    paired = sorted(
        ((c, y) for c, y in zip(confidences, correct, strict=True)),
        key=lambda p: p[0],
    )
    n = len(paired)
    if n == 0:
        return []
    n_bins = max(1, min(n_bins, n))
    bins: list[ReliabilityBin] = []
    for b in range(n_bins):
        lo = (b * n) // n_bins
        hi = ((b + 1) * n) // n_bins
        chunk = paired[lo:hi]
        if not chunk:
            continue
        confs = [c for c, _ in chunk]
        ys = [y for _, y in chunk]
        bins.append(
            ReliabilityBin(
                confidence=statistics.fmean(confs),
                accuracy=statistics.fmean(ys),
                count=len(chunk),
            )
        )
    return bins


def _ece_from_bins(bins: Sequence[ReliabilityBin], total: int) -> float:
    """Expected Calibration Error: population-weighted ``|confidence - accuracy|`` over bins."""
    if total <= 0:
        return 0.0
    return sum(b.count * abs(b.confidence - b.accuracy) for b in bins) / total


def _ece_ci(
    confidences: Sequence[float],
    correct: Sequence[float],
    *,
    n_bins: int,
    alpha: float,
    n_resamples: int,
    seed: int,
) -> tuple[float, float]:
    """Bootstrap ``(lo, hi)`` CI for ECE (resample points, re-bin, re-compute).

    Uses a local seeded ``random.Random`` (never the global RNG): identical inputs ⇒
    identical CI. Re-binning each resample makes the CI honest about the binning choice.
    """
    n = len(confidences)
    if n == 0:
        return (0.0, 0.0)
    rng = random.Random(seed)
    eces: list[float] = []
    for _ in range(n_resamples):
        idx = [rng.randrange(n) for _ in range(n)]
        rs_conf = [confidences[i] for i in idx]
        rs_corr = [correct[i] for i in idx]
        bins = _equal_mass_bins(rs_conf, rs_corr, n_bins=n_bins)
        eces.append(_ece_from_bins(bins, len(rs_conf)))
    eces.sort()
    lo = _percentile(eces, alpha / 2.0)
    hi = _percentile(eces, 1.0 - alpha / 2.0)
    return (lo, hi)


def _percentile(sorted_values: list[float], q: float) -> float:
    """Linear-interpolated quantile of an already-sorted list (``q`` in ``[0,1]``)."""
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    rank = q * (len(sorted_values) - 1)
    lo_idx = int(rank)
    hi_idx = min(lo_idx + 1, len(sorted_values) - 1)
    frac = rank - lo_idx
    return sorted_values[lo_idx] * (1.0 - frac) + sorted_values[hi_idx] * frac


def _content_sha_for_calibrate(
    definition: Definition, base_seed: int, runs: int, case_ids: Sequence[str]
) -> str:
    """Content hash over the calibration coordinates (definition + seed + runs + cases)."""
    payload = {
        "definition": definition.id,
        "version": str(definition.version),
        "base_seed": base_seed,
        "runs": runs,
        "cases": sorted(case_ids),
    }
    blob = json.dumps(payload, sort_keys=True, default=str).encode()
    return hashlib.sha256(blob).hexdigest()[:16]


async def calibrate(
    definition: Definition,
    golden: GoldenSet | Sequence[EvalCase],
    *,
    runs: int = 5,
    ctx: RunContext,
    runtime: AgentRuntime,
    rubric: Rubric | None = None,
    confidence_field: str = "confidence",
    cost_per_run_usd: float = 0.0,
    target_accuracy: float = 0.9,
    n_bins: int = 10,
    alpha: float = 0.05,
    n_resamples: int = 1000,
    base_seed: int = 0,
    inputs_for: Callable[[EvalCase], dict[str, JSONValue]] | None = None,
) -> CalibrationReport:
    """Run each golden case ``runs`` times under distinct derived seeds → a report.

    For each case (sorted by id), execute the Definition ``runs`` times against ``runtime``,
    each run carrying a per-run ``decode_seed`` derived purely from ``base_seed`` (so the
    same ``(base_seed, runs)`` reproduces the seed schedule, and a seed-honouring runtime
    varies its decode per run). The rubric scores every output; per-metric mean/std is the
    noise band; structural disagreement across a case's re-runs is the ``output_variance``.
    When cases carry labels, confidence (via :func:`crawfish.escalate.extract_confidence`)
    is calibrated against correctness — Brier (primary), ECE + bootstrap CI (diagnostic), a
    reliability curve, and an evidence-derived abstention threshold/rate.

    **Refuses a ``RecordReplayRuntime``** (raises :class:`CalibrationError`): replay zeroes
    variance, so calibrating over it would be a fabricated zero-noise report.

    **Bounded** by ``runs × len(golden)`` and the autonomy ceiling: each run charges
    ``cost_per_run_usd`` against ``ctx.cost_budget`` and checks ``ctx.cancel_token``; a
    ceiling breach returns a **partial** report over what was measured (``partial=True``),
    the Tuner's ceiling-returns-base analogue — calibrate never spends unbounded cost.

    Deterministic everywhere except the model call: seed derivation, aggregation, std,
    Brier, ECE and its bootstrap CI are pure arithmetic over a seeded local RNG.
    """
    if runs < 1:
        raise ValueError(f"runs must be >= 1, got {runs!r}")
    # Refuse replay: it would silently report zero variance (the one runtime calibrate may
    # legitimately drive non-replayed). Import lazily to avoid a hard runtime dependency.
    from crawfish.runtime.replay import RecordReplayRuntime

    if isinstance(runtime, RecordReplayRuntime):
        raise CalibrationError(
            "cw.calibrate cannot measure variance against a RecordReplayRuntime — replay "
            "returns a recorded result with zero run-to-run variance; calibrate over a live "
            "(non-replay) runtime instead"
        )

    cases = _golden_cases(golden)
    active_rubric = rubric or Rubric([is_nonempty()])
    bind = inputs_for or (lambda c: dict(c.inputs))

    metric_names = [m.name for m in active_rubric.metrics]
    per_metric_scores: dict[str, list[float]] = {name: [] for name in metric_names}
    per_case_values: list[list[JSONValue]] = []
    confidences: list[float] = []
    correctness: list[float] = []
    has_labels = False
    partial = False

    for case in cases:
        if partial:
            break
        case_values: list[JSONValue] = []
        for i in range(runs):
            # Autonomy ceiling: cancel first (kill-switch), then budget — checked BEFORE the
            # run so we never spend past the ceiling; a breach yields a partial report.
            if ctx.cancel_token.cancelled:
                partial = True
                break
            if cost_per_run_usd:
                try:
                    ctx.cost_budget.charge(cost_per_run_usd)
                except BudgetExceeded:
                    partial = True
                    break

            inputs = bind(case)
            request = RunRequest(
                definition=definition,
                inputs=inputs,
                decode_seed=_run_seed(base_seed, case.id, i),
            )
            result = await runtime.run(request, ctx)
            output = _output_from_result(definition, inputs, result)

            scored = active_rubric.score(output)
            for name, value in scored.items():
                per_metric_scores[name].append(value)
            case_values.append(canonicalize(output.value))

            if case.label is not None:
                has_labels = True
                conf = extract_confidence(output, field=confidence_field)
                if conf is not None:
                    confidences.append(conf)
                    correctness.append(_correct(output, case.label))
        if case_values:
            per_case_values.append(case_values)

    # -- aggregate the noise band (pure) ------------------------------------
    rubric_mean: dict[str, float] = {}
    rubric_std: dict[str, float] = {}
    for name, values in per_metric_scores.items():
        if values:
            rubric_mean[name] = statistics.fmean(values)
            rubric_std[name] = statistics.pstdev(values) if len(values) > 1 else 0.0
        else:
            rubric_mean[name] = 0.0
            rubric_std[name] = 0.0

    output_variance = (
        statistics.fmean(_structural_disagreement(v) for v in per_case_values)
        if per_case_values
        else 0.0
    )

    # -- determinism tier + infra variance floor (F-5) ----------------------
    tier = getattr(runtime, "determinism_tier", DeterminismTier.BEST_EFFORT)
    # A backend that does not honour the seed cannot attribute its variance to the model;
    # the observed output variance is the floor we attribute to infra, not the Definition.
    infra_floor = output_variance if tier is not DeterminismTier.HONORS_SEED else 0.0

    # -- calibration metrics (only with labels) -----------------------------
    brier: float | None = None
    ece: float | None = None
    ece_ci: tuple[float, float] | None = None
    reliability: tuple[ReliabilityBin, ...] = ()
    abstain_threshold = 1.0
    abstain_rate = 0.0

    if has_labels and confidences:
        brier = _brier(confidences, correctness)
        bins = _equal_mass_bins(confidences, correctness, n_bins=n_bins)
        reliability = tuple(bins)
        ece = _ece_from_bins(bins, len(confidences))
        ece_ci = _ece_ci(
            confidences,
            correctness,
            n_bins=n_bins,
            alpha=alpha,
            n_resamples=n_resamples,
            seed=base_seed,
        )
        abstain_threshold = abstention_threshold(
            [b.confidence for b in bins],
            [b.accuracy for b in bins],
            [b.count for b in bins],
            target=target_accuracy,
        )
        below = sum(1 for c in confidences if c < abstain_threshold)
        abstain_rate = below / len(confidences)

    return CalibrationReport(
        org_id=ctx.org_id,
        definition_id=definition.id,
        definition_version=str(definition.version),
        content_sha=_content_sha_for_calibrate(definition, base_seed, runs, [c.id for c in cases]),
        base_seed=base_seed,
        runs=runs,
        cases=len(cases),
        determinism_tier=tier,
        rubric_mean=rubric_mean,
        rubric_std=rubric_std,
        output_variance=output_variance,
        infra_variance_floor=infra_floor,
        brier=brier,
        ece=ece,
        ece_ci=ece_ci,
        reliability=reliability,
        abstention_threshold=abstain_threshold,
        abstention_rate=abstain_rate,
        partial=partial,
    )
