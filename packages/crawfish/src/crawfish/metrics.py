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

import json
import re
from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence

from crawfish.batch import Task
from crawfish.core.context import RunContext
from crawfish.core.types import JSONValue, Parameter
from crawfish.definition.types import Definition
from crawfish.experiment import k_from_alpha
from crawfish.output import Output
from crawfish.run import Run
from crawfish.runtime.base import AgentRuntime
from crawfish.validation import canonicalize, structural_diff, validate_output

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
