"""Grammar — a static, trusted constraint on the decode surface (CRA-218 / TS-8).

Constrained decoding is *strictly stronger* than the post-hoc validate+repair loop
(``run.py``): instead of detecting a malformed output and paying a metered repair call
to fix it, the runtime is told the **shape** the output must take and a malformed value
becomes an *impossible* state. This module is the portable, provider-neutral
representation of that shape.

Three things live here:

* :class:`Grammar` — a frozen, declarative constraint (``ENUM`` / ``REGEX`` /
  ``JSON_SCHEMA``). It is **static and author-supplied** — built from a Definition's
  declared output schema or from author constants, *never* from a fluid (untrusted)
  value. A fluid value cannot set the grammar (it has no constructor that reads one);
  that keeps the prompt-injection boundary intact: the constraint is trusted config.
* :meth:`Grammar.to_request_grammar` / :func:`parse` — the (lossless) serialization to
  and from the per-call ``RunRequest.grammar`` dialect string. That string is a *per-call*
  property kept **out of** the Definition content hash (ADR 0017 / F-5): it constrains the
  decode surface, it does not version the agent.
* :meth:`Grammar.enforce` — a **pure, deterministic** projection of arbitrary text onto
  the constraint surface. A runtime that "honours" a grammar applies this to produce a
  structured field; the same text + same grammar always yields the same constrained
  value (so a constrained decode is bit-for-bit reproducible alongside ``decode_seed``).

``enforce`` is deterministic and total on every input the constraint can satisfy; when no
candidate exists at all it raises :class:`GrammarError` (the constraint is genuinely
unsatisfiable for that text), never a silent coercion.
"""

from __future__ import annotations

import json
import re
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from crawfish.core.types import JSONValue, Parameter

__all__ = [
    "GrammarKind",
    "Grammar",
    "GrammarError",
    "parse",
]


class GrammarError(ValueError):
    """Raised when text cannot be projected onto a constraint surface at all."""


def _first_object_span(text: str) -> str | None:
    """Return the first balanced ``{...}`` span in ``text`` (brace-depth scan), or None.

    Pure and deterministic — recovers a JSON object the backend wrapped in prose, the
    same span the output validator would extract. Quote/escape aware so a ``}`` inside a
    string literal does not close the object early.
    """
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


class GrammarKind(str, Enum):
    """The dialect of a :class:`Grammar`. ``(str, Enum)`` per ADR 0004."""

    ENUM = "enum"  # one of a fixed, ordered set of literal choices
    REGEX = "regex"  # the output must match an (anchored) regular expression
    JSON_SCHEMA = "json_schema"  # the output is a JSON object with declared keys


# Serialization separators for ``to_request_grammar``/``parse``. The dialect string is
# ``"<kind>:<body>"``; ENUM choices are joined on a unit-separator that cannot occur in a
# bare identifier, so round-tripping is lossless for the constraints we mint.
_KIND_SEP = ":"
_ENUM_SEP = "\x1f"  # ASCII unit separator — not a plausible literal choice character


class Grammar(BaseModel):
    """A frozen, declarative constraint on a single decoded field.

    Construct via the classmethods (:meth:`enum`, :meth:`regex`, :meth:`json_object`,
    :meth:`from_output_schema`) rather than the raw initializer — they keep the
    kind/body invariant. Frozen so a constraint cannot be mutated after a runtime has
    keyed a cassette on it.
    """

    model_config = ConfigDict(frozen=True)

    kind: GrammarKind
    # ENUM: the ordered set of acceptable literals.
    choices: tuple[str, ...] = Field(default_factory=tuple)
    # REGEX: the (treated-as-anchored) pattern source.
    pattern: str = ""
    # JSON_SCHEMA: the declared object keys (required field names).
    keys: tuple[str, ...] = Field(default_factory=tuple)

    # -- constructors -------------------------------------------------------
    @classmethod
    def enum(cls, choices: list[str] | tuple[str, ...]) -> Grammar:
        """A constraint to one of ``choices`` (order preserved; first wins on ties)."""
        items = tuple(choices)
        if not items:
            raise ValueError("enum grammar needs at least one choice")
        return cls(kind=GrammarKind.ENUM, choices=items)

    @classmethod
    def regex(cls, pattern: str) -> Grammar:
        """A constraint that the output match ``pattern`` (compiled, validated now)."""
        re.compile(pattern)  # fail fast on a malformed author pattern
        return cls(kind=GrammarKind.REGEX, pattern=pattern)

    @classmethod
    def json_object(cls, keys: list[str] | tuple[str, ...]) -> Grammar:
        """A constraint that the output be a JSON object carrying (at least) ``keys``."""
        return cls(kind=GrammarKind.JSON_SCHEMA, keys=tuple(keys))

    @classmethod
    def from_output_schema(cls, outputs: list[Parameter]) -> Grammar | None:
        """Derive a grammar from a Definition's **declared, trusted** output schema.

        The schema is author config (static), so the derived grammar is trusted too.
        Returns ``None`` for a schema there is nothing to constrain (empty, or a single
        free-text ``str`` output) — the caller then leaves ``grammar=None`` and the run
        degrades to the ordinary validate path.

        * A multi-field schema → a ``JSON_SCHEMA`` grammar keyed by the declared output
          names (the object the model must return).
        * A single non-``str`` declared output → also a ``JSON_SCHEMA`` over that one
          name, so the structured field is produced under constraint.
        """
        if not outputs:
            return None
        if len(outputs) == 1 and outputs[0].type == "str":
            return None
        return cls.json_object([p.name for p in outputs])

    # -- serialization (per-call dialect string, OUT of the content hash) ---
    def to_request_grammar(self) -> str:
        """Serialize to the per-call ``RunRequest.grammar`` dialect string.

        This is the value that rides on the *request*, never the Definition — it does
        not enter ``content_dict()`` / ``version.sha`` (ADR 0017 / F-5).
        """
        if self.kind is GrammarKind.ENUM:
            body = _ENUM_SEP.join(self.choices)
        elif self.kind is GrammarKind.REGEX:
            body = self.pattern
        else:  # JSON_SCHEMA
            body = _ENUM_SEP.join(self.keys)
        return f"{self.kind.value}{_KIND_SEP}{body}"

    # -- enforcement (pure, deterministic projection) -----------------------
    def satisfies(self, text: str) -> bool:
        """True if ``text`` already meets the constraint (no projection needed)."""
        if self.kind is GrammarKind.ENUM:
            return text in self.choices
        if self.kind is GrammarKind.REGEX:
            return re.fullmatch(self.pattern, text) is not None
        # JSON_SCHEMA: parseable object carrying every declared key.
        try:
            parsed = json.loads(text)
        except (ValueError, TypeError):
            return False
        return isinstance(parsed, dict) and all(k in parsed for k in self.keys)

    def enforce(self, text: str) -> str:
        """Project arbitrary ``text`` onto the constraint surface, deterministically.

        Pure and reproducible: the same ``text`` + same grammar always yields the same
        constrained string. This is what a grammar-honouring runtime applies so a
        structured field is *guaranteed* well-formed — moving a malformed output from a
        retried failure (a metered ``_repair`` call) to an impossible state.

        Raises :class:`GrammarError` only when no candidate exists at all.
        """
        if self.kind is GrammarKind.ENUM:
            return self._enforce_enum(text)
        if self.kind is GrammarKind.REGEX:
            return self._enforce_regex(text)
        return self._enforce_json(text)

    def _enforce_enum(self, text: str) -> str:
        if text in self.choices:
            return text
        # Deterministic snap: the first declared choice that appears as a substring,
        # else (no candidate present) the first declared choice — a constrained decode
        # cannot emit an out-of-set token, so the field is always a valid member.
        for choice in self.choices:
            if choice in text:
                return choice
        return self.choices[0]

    def _enforce_regex(self, text: str) -> str:
        if re.fullmatch(self.pattern, text) is not None:
            return text
        match = re.search(self.pattern, text)
        if match is None:
            raise GrammarError(f"text does not contain any substring matching /{self.pattern}/")
        return match.group(0)

    def _enforce_json(self, text: str) -> str:
        """Coerce to a JSON object carrying exactly the declared keys, deterministically.

        The genuine repair this eliminates is the *parse* failure: a constrained-decode
        backend emits a JSON object, but a free-form one may wrap it in prose. We recover
        the first balanced ``{...}`` span (the same span the validator would look for),
        keep the declared keys in canonical order, and drop the rest — a pure function of
        the input, so the projection is reproducible. A declared key the backend omitted
        is *not* synthesised with a dummy value (that would fake a typed field the model
        never produced); the validator still checks per-key types, which is the runtime's
        constrained-decode responsibility, not this portable string projection.
        """
        span = _first_object_span(text)
        try:
            parsed: JSONValue = json.loads(span) if span is not None else None
        except (ValueError, TypeError):
            parsed = None
        obj: dict[str, JSONValue] = parsed if isinstance(parsed, dict) else {}
        constrained: dict[str, JSONValue] = {k: obj[k] for k in self.keys if k in obj}
        return json.dumps(constrained, sort_keys=True, separators=(",", ":"))


def parse(serialized: str) -> Grammar:
    """Read a per-call ``RunRequest.grammar`` dialect string back into a :class:`Grammar`.

    The inverse of :meth:`Grammar.to_request_grammar`. A runtime that mediates the
    constraint reads the request's grammar string through this to recover the typed
    constraint, then applies :meth:`Grammar.enforce`.
    """
    kind_str, sep, body = serialized.partition(_KIND_SEP)
    if not sep:
        raise GrammarError(f"malformed grammar dialect string: {serialized!r}")
    try:
        kind = GrammarKind(kind_str)
    except ValueError as exc:
        raise GrammarError(f"unknown grammar kind {kind_str!r}") from exc
    if kind is GrammarKind.ENUM:
        return Grammar.enum(tuple(body.split(_ENUM_SEP)))
    if kind is GrammarKind.REGEX:
        return Grammar.regex(body)
    keys = tuple(body.split(_ENUM_SEP)) if body else ()
    return Grammar.json_object(keys)
