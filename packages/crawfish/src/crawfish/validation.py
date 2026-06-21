"""Typed input/output validation contracts.

``run.py`` constructs the result envelope as ``Output(value=result.text, ...)`` — every
output is a **string**, which is why diffs/evals are weak and complex output types don't
exist. ``Definition.outputs`` is already a ``list[Parameter]`` (the declared schema) and
``Output[T]`` is generic + frozen, but nothing validates ``result.text`` against that
schema, and ``run.validate()`` only checks input *presence*, not value/type.

CRA-184 freezes the **signatures**; CRA-172 implements them:

* :func:`validate_output` — parse a model's text against the declared output schema,
  returning the typed value and a list of structured failures.
* :func:`validate_inputs` — validate bound input *values* (not just presence) against the
  input schema. (Tool/MCP results re-enter the model as content and are untrusted too —
  the caller marks the resulting value tainted; see SECURITY.md.)
* :func:`structural_diff` — a typed, order-canonical diff between two values, the basis
  for eval scoring (#5) and the tuner's regression checks (#6).

The ``Output.value`` contract (inline typed value vs :class:`~crawfish.artifacts.ArtifactRef`)
is settled in ADR 0013: the value is **inline** by default; an ``ArtifactRef`` is an explicit
opt-in for large blobs, dereferenced at a single point. Validators operate on the inline value.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import Enum

from pydantic import BaseModel, Field

from crawfish.core.types import JSONValue, Parameter
from crawfish.typesystem.registry import TypeRegistry

__all__ = [
    "ValidationFailure",
    "ValidationError",
    "StructuralDiff",
    "validate_output",
    "validate_inputs",
    "structural_diff",
]


class ValidationFailure(str, Enum):
    """The closed set of structured validation failure reasons."""

    NOT_JSON = "not_json"  # text was expected to be JSON and was not parseable
    MISSING_FIELD = "missing_field"  # a required schema field was absent
    TYPE_MISMATCH = "type_mismatch"  # a value's type was not registry-compatible
    EXTRA_FIELD = "extra_field"  # a field not in the schema was present (strict mode)
    EMPTY_SCHEMA = "empty_schema"  # no output schema declared to validate against
    CONSTRAINT = "constraint"  # a declared constraint (range/enum/etc.) was violated


class ValidationError(BaseModel):
    """One structured validation failure. Frozen."""

    failure: ValidationFailure
    field: str | None = None  # dotted path to the offending field, if any
    detail: str = ""  # human-readable explanation (never contains secret values)

    model_config = {"frozen": True}


class StructuralDiff(BaseModel):
    """A typed, order-canonical difference between two values. Frozen.

    ``added``/``removed``/``changed`` are dotted field paths. ``equal`` is the
    convenience predicate eval scoring keys off of.
    """

    added: tuple[str, ...] = Field(default_factory=tuple)
    removed: tuple[str, ...] = Field(default_factory=tuple)
    changed: tuple[str, ...] = Field(default_factory=tuple)

    model_config = {"frozen": True}

    @property
    def equal(self) -> bool:
        """True when there are no additions, removals, or changes."""
        return not (self.added or self.removed or self.changed)


def validate_output(
    text: str,
    outputs: list[Parameter],
    reg: TypeRegistry | None = None,
) -> tuple[JSONValue, list[ValidationError]]:
    """Parse and validate a model's ``text`` against the declared ``outputs`` schema.

    Returns ``(value, errors)``: the typed value (best-effort parsed) and a list of
    structured failures (empty when valid). Implemented in CRA-172.
    """
    raise NotImplementedError("validate_output is implemented in CRA-172")


def validate_inputs(
    values: Mapping[str, JSONValue],
    schema: list[Parameter],
    reg: TypeRegistry | None = None,
) -> list[ValidationError]:
    """Validate bound input ``values`` against the input ``schema`` (presence + type).

    Unlike the current presence-only ``run.validate()``, this checks each value's type
    against its ``Parameter.type`` via the registry. Implemented in CRA-172.
    """
    raise NotImplementedError("validate_inputs is implemented in CRA-172")


def structural_diff(
    before: JSONValue,
    after: JSONValue,
    *,
    schema: list[Parameter] | None = None,
    reg: TypeRegistry | None = None,
) -> StructuralDiff:
    """Compute an order-canonical structural diff between two values.

    Unordered collections are canonicalized before comparison so the diff is
    deterministic under record/replay. Implemented in CRA-172.
    """
    raise NotImplementedError("structural_diff is implemented in CRA-172")
