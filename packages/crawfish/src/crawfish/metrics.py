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
from crawfish.core.types import JSONValue
from crawfish.definition.types import Definition
from crawfish.output import Output
from crawfish.run import Run
from crawfish.runtime.base import AgentRuntime

__all__ = [
    "Metric",
    "Rubric",
    "Benchmark",
    "OutputNumber",
    "FieldPresent",
    "IsNonempty",
    "ConfidenceThreshold",
    "output_number",
    "field_present",
    "is_nonempty",
    "confidence_threshold",
    "compare",
    "is_regression",
]

_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")


def _as_mapping(value: object) -> dict[str, JSONValue] | None:
    """View an Output value as a string->value mapping for field metrics.

    Post-CRA-172 a Definition with a typed (RECORD) output schema already carries a
    ``dict`` in ``Output.value`` — that path is read directly with no decoding. The
    string-decode fallback survives only for the **back-compat** case: a Definition with
    *no* declared outputs keeps a plain-string ``Output.value`` (``RunResult.text``);
    when that string JSON-encodes an object we decode it so field metrics still apply.
    """
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (ValueError, TypeError):
            return None
        if isinstance(decoded, dict):
            return decoded
    return None


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


# -- factory aliases (Metric subclasses, exposed as ergonomic factories) -----
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
