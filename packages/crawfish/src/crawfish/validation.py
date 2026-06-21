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

Extraction strategy (``claude -p`` has **no JSON mode**)
-------------------------------------------------------
``CommandRuntime`` returns free text. :func:`validate_output` therefore parses JSON *out of*
the text tolerantly: it strips Markdown code fences and isolates the outermost ``{...}`` /
``[...]`` span before decoding. A single ``str``-typed output (or an empty schema) is a
pass-through: the raw text is the value and no JSON parse is attempted (back-compat — a Run
with no declared outputs keeps a string ``Output.value``).

``ValidationFailure`` is the closed set of failure *reasons*. The retry/repair/dead-letter
*action* policy is a distinct concern: :class:`ValidationAction` (used by ``run.py``).
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from crawfish.core.types import JSONValue, Parameter
from crawfish.typesystem.registry import TypeDef, TypeKind, TypeRegistry, default_registry

__all__ = [
    "ValidationFailure",
    "ValidationAction",
    "ValidationError",
    "StructuralDiff",
    "validate_output",
    "validate_inputs",
    "structural_diff",
    "canonicalize",
]


class ValidationFailure(str, Enum):
    """The closed set of structured validation failure reasons."""

    NOT_JSON = "not_json"  # text was expected to be JSON and was not parseable
    MISSING_FIELD = "missing_field"  # a required schema field was absent
    TYPE_MISMATCH = "type_mismatch"  # a value's type was not registry-compatible
    EXTRA_FIELD = "extra_field"  # a field not in the schema was present (strict mode)
    EMPTY_SCHEMA = "empty_schema"  # no output schema declared to validate against
    CONSTRAINT = "constraint"  # a declared constraint (range/enum/etc.) was violated


class ValidationAction(str, Enum):
    """The *action* policy applied when validation fails — distinct from the failure
    *reason* (:class:`ValidationFailure`). ``run.py`` reads this to decide whether to
    retry the run, re-prompt the model to repair its output, or dead-letter the item.
    """

    RETRY = "retry"  # re-run via the RetryPolicy (transient failure)
    REPAIR = "repair"  # re-prompt the model with the schema error (one extra call)
    DEAD_LETTER = "dead_letter"  # give up and record for later replay


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


# -- canonicalisation -------------------------------------------------------
def canonicalize(value: JSONValue) -> JSONValue:
    """Return ``value`` with every mapping's keys recursively sorted.

    Records are *unordered*; sorting keys makes equality and diffs deterministic
    under record/replay so golden-set comparisons are reproducible. Lists keep
    their order (they are ordered collections).
    """
    if isinstance(value, Mapping):
        return {k: canonicalize(value[k]) for k in sorted(value)}
    if isinstance(value, (list, tuple)):
        return [canonicalize(v) for v in value]
    return value


# -- text → JSON extraction -------------------------------------------------
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)


def _extract_json(text: str) -> tuple[JSONValue, bool]:
    """Tolerantly parse a JSON value out of model ``text``.

    Returns ``(value, ok)``. Strips a fenced code block first, then tries a whole
    parse, then isolates the outermost ``{...}`` / ``[...]`` span. ``ok`` is False
    when nothing parseable was found.
    """
    candidates: list[str] = []
    fence = _FENCE_RE.search(text)
    if fence is not None:
        candidates.append(fence.group(1).strip())
    stripped = text.strip()
    candidates.append(stripped)

    span = _outermost_span(stripped)
    if span is not None:
        candidates.append(span)

    for candidate in candidates:
        if not candidate:
            continue
        try:
            return json.loads(candidate), True
        except (ValueError, TypeError):
            continue
    return None, False


def _outermost_span(text: str) -> str | None:
    """Return the substring from the first ``{``/``[`` to its matching close."""
    starts = [i for i in (text.find("{"), text.find("[")) if i != -1]
    if not starts:
        return None
    start = min(starts)
    opener = text[start]
    closer = "}" if opener == "{" else "]"
    depth = 0
    in_str = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


# -- registry-driven structural type check ----------------------------------
def _is_str_passthrough(schema: list[Parameter]) -> bool:
    """True when the schema is a single ``str`` primitive — the pass-through case.

    Such an output is plain text, not JSON, so we never attempt a JSON parse and
    the raw model text becomes the value (back-compat with the string-output era).
    """
    if len(schema) != 1:
        return False
    return schema[0].type.strip() == "str"


def _typeof(value: JSONValue) -> str:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    if value is None:
        return "null"
    if isinstance(value, list):
        return "list"
    if isinstance(value, Mapping):
        return "record"
    return type(value).__name__


def _check_value(
    value: JSONValue,
    td: TypeDef,
    reg: TypeRegistry,
    *,
    path: str,
    errors: list[ValidationError],
) -> None:
    """Walk a resolved :class:`TypeDef`, appending a structured error per mismatch."""
    if td.kind is TypeKind.OPTIONAL:
        assert td.item is not None
        if value is None:
            return
        _check_value(value, reg.resolve(td.item), reg, path=path, errors=errors)
        return

    if td.kind is TypeKind.LIST:
        assert td.item is not None
        if not isinstance(value, list):
            errors.append(
                ValidationError(
                    failure=ValidationFailure.TYPE_MISMATCH,
                    field=path or None,
                    detail=f"expected list, got {_typeof(value)}",
                )
            )
            return
        item_td = reg.resolve(td.item)
        for i, elem in enumerate(value):
            _check_value(elem, item_td, reg, path=f"{path}[{i}]", errors=errors)
        return

    if td.kind is TypeKind.RECORD:
        if not isinstance(value, Mapping):
            errors.append(
                ValidationError(
                    failure=ValidationFailure.TYPE_MISMATCH,
                    field=path or None,
                    detail=f"expected record '{td.name}', got {_typeof(value)}",
                )
            )
            return
        for fname, ftype in td.fields.items():
            child = f"{path}.{fname}" if path else fname
            if fname not in value:
                errors.append(
                    ValidationError(
                        failure=ValidationFailure.MISSING_FIELD,
                        field=child,
                        detail=f"required field '{fname}' is absent",
                    )
                )
                continue
            _check_value(value[fname], reg.resolve(ftype), reg, path=child, errors=errors)
        return

    # primitive (possibly nominal): match the JSON value against the primitive name.
    _check_primitive(value, td.name, path=path, errors=errors)


# Which JSON python types satisfy each primitive name. ``json`` accepts anything;
# unknown (nominal) primitive names accept any non-container scalar.
def _check_primitive(
    value: JSONValue,
    name: str,
    *,
    path: str,
    errors: list[ValidationError],
) -> None:
    ok: bool
    if name == "json":
        ok = True
    elif name == "str":
        ok = isinstance(value, str)
    elif name == "bool":
        ok = isinstance(value, bool)
    elif name == "int":
        ok = isinstance(value, int) and not isinstance(value, bool)
    elif name == "float":
        # ints are valid floats (JSON has one number type)
        ok = isinstance(value, (int, float)) and not isinstance(value, bool)
    elif name == "null":
        ok = value is None
    else:
        # nominal primitive: accept any scalar (not list/record)
        ok = not isinstance(value, (list, Mapping))
    if not ok:
        errors.append(
            ValidationError(
                failure=ValidationFailure.TYPE_MISMATCH,
                field=path or None,
                detail=f"expected {name}, got {_typeof(value)}",
            )
        )


# -- public API -------------------------------------------------------------
def validate_output(
    text: str,
    outputs: list[Parameter],
    reg: TypeRegistry | None = None,
) -> tuple[JSONValue, list[ValidationError]]:
    """Parse and validate a model's ``text`` against the declared ``outputs`` schema.

    Returns ``(value, errors)``: the typed value (best-effort parsed, canonicalised)
    and a list of structured failures (empty when valid).

    * **No declared outputs** → pass-through: ``(text, [])``. A Run with no schema keeps
      a plain-string ``Output.value`` (back-compat); there is nothing to validate, so no
      ``EMPTY_SCHEMA`` error is raised on this routine no-schema path.
    * **Single ``str`` output** → pass-through: ``(text, [])`` (text is not JSON).
    * **Otherwise** → extract JSON from the text and validate each declared output field.
      An unparseable payload yields ``(text, [NOT_JSON])``.
    """
    registry = reg or default_registry

    if not outputs:
        return text, []

    if _is_str_passthrough(outputs):
        return text, []

    parsed, ok = _extract_json(text)
    if not ok:
        return text, [
            ValidationError(
                failure=ValidationFailure.NOT_JSON,
                detail="model output was not parseable as JSON",
            )
        ]

    value = canonicalize(parsed)
    errors: list[ValidationError] = []

    if len(outputs) == 1:
        # A single declared output IS the whole value (record/list/primitive).
        _check_value(value, registry.resolve(outputs[0].type), registry, path="", errors=errors)
        return value, errors

    # Multiple declared outputs → the value must be a record keyed by output name.
    if not isinstance(value, Mapping):
        errors.append(
            ValidationError(
                failure=ValidationFailure.TYPE_MISMATCH,
                detail=f"expected an object with fields {[p.name for p in outputs]},"
                f" got {_typeof(value)}",
            )
        )
        return value, errors
    for param in outputs:
        if param.name not in value:
            if param.required and param.default is None:
                errors.append(
                    ValidationError(
                        failure=ValidationFailure.MISSING_FIELD,
                        field=param.name,
                        detail=f"required output '{param.name}' is absent",
                    )
                )
            continue
        _check_value(
            value[param.name],
            registry.resolve(param.type),
            registry,
            path=param.name,
            errors=errors,
        )
    return value, errors


def validate_inputs(
    values: Mapping[str, JSONValue],
    schema: list[Parameter],
    reg: TypeRegistry | None = None,
) -> list[ValidationError]:
    """Validate bound input ``values`` against the input ``schema`` (presence + type).

    Unlike the presence-only ``run.validate()``, this checks each value's type against
    its ``Parameter.type`` via the registry. A missing required input → ``MISSING_FIELD``;
    a wrong-typed input → ``TYPE_MISMATCH``. Returns the (possibly empty) error list.
    """
    registry = reg or default_registry
    errors: list[ValidationError] = []
    for param in schema:
        if param.name not in values:
            if param.required and param.default is None:
                errors.append(
                    ValidationError(
                        failure=ValidationFailure.MISSING_FIELD,
                        field=param.name,
                        detail=f"required input '{param.name}' is unbound",
                    )
                )
            continue
        _check_value(
            values[param.name],
            registry.resolve(param.type),
            registry,
            path=param.name,
            errors=errors,
        )
    return errors


def structural_diff(
    before: JSONValue,
    after: JSONValue,
    *,
    schema: list[Parameter] | None = None,
    reg: TypeRegistry | None = None,
) -> StructuralDiff:
    """Compute an order-canonical structural diff between two values.

    Records are canonicalised (keys sorted) before comparison so the diff is
    deterministic under record/replay. ``added``/``removed``/``changed`` hold dotted
    field paths (``a.b``, ``a[0]``). ``schema``/``reg`` are accepted for symmetry with
    the other validators and to satisfy the frozen signature; the diff itself is
    structural and does not need them.
    """
    _ = (schema, reg)  # reserved; the diff is purely structural
    added: list[str] = []
    removed: list[str] = []
    changed: list[str] = []
    _diff(canonicalize(before), canonicalize(after), "", added, removed, changed)
    return StructuralDiff(
        added=tuple(sorted(added)),
        removed=tuple(sorted(removed)),
        changed=tuple(sorted(changed)),
    )


def _diff(
    before: Any,
    after: Any,
    path: str,
    added: list[str],
    removed: list[str],
    changed: list[str],
) -> None:
    if isinstance(before, Mapping) and isinstance(after, Mapping):
        for key in sorted(set(before) | set(after)):
            child = f"{path}.{key}" if path else str(key)
            if key not in before:
                added.append(child)
            elif key not in after:
                removed.append(child)
            else:
                _diff(before[key], after[key], child, added, removed, changed)
        return
    if isinstance(before, list) and isinstance(after, list):
        for i in range(max(len(before), len(after))):
            child = f"{path}[{i}]"
            if i >= len(before):
                added.append(child)
            elif i >= len(after):
                removed.append(child)
            else:
                _diff(before[i], after[i], child, added, removed, changed)
        return
    if before != after:
        changed.append(path)
