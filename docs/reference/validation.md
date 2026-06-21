# Validation

How a node's *actual* output and inputs are checked against the `Parameter`
schema it declared — what failed, why, and what to do about it. These live in
`crawfish.validation` and run at the boundary between free model text and the
typed values a pipeline expects.

**Symbols on this page:** `ValidationFailure` · `ValidationAction` · `ValidationError` ·
`StructuralDiff` · `validate_output` · `validate_inputs` · `structural_diff`

---

## Core

A node declares its ports as [`Parameter`](core-types.md)s: each has a `name` and a
`type` string like `"str"` or `"int"`. But declaring a schema is not the same as
*meeting* it. The model returns free text; bound inputs may be the wrong shape.
Validation closes that gap — it parses the real value and compares it field-by-field
against the declared schema.

Two entry points do the comparing:

- **`validate_output`** takes the model's raw text and the declared output schema,
  and returns *(value, errors)*: the parsed value plus a list of everything that
  didn't conform.
- **`validate_inputs`** takes the values bound to a node's inputs and the input
  schema, and returns the list of errors. (Unlike a presence-only check, it verifies
  each value's *type*, not just that it is present.)

When something is wrong, each problem is recorded as a **`ValidationError`** — despite
the name, this is a *data record*, not a Python exception you catch. It names the
failure reason, the offending field, and a human-readable detail. The reason is drawn
from a fixed list, **`ValidationFailure`** — for example "a required field was absent"
or "a value's type didn't match."

Knowing *what* failed is separate from deciding *what to do* about it. That decision is
**`ValidationAction`**: retry the run, re-prompt the model to repair its output, or give
up and set the item aside for later. The validators never pick an action; they only
report failures. The action policy is applied elsewhere (by the run executor).

Finally, **`structural_diff`** answers a related question: how do two values differ?
It returns a **`StructuralDiff`** listing which field paths were added, removed, or
changed between a *before* and an *after* value — the basis for scoring an output
against a known-good answer.

---

## Ramps up

### Why output is text, and what that forces

A run's `Output.value` is a string by default — `claude -p` returns free text and has
no JSON mode. `validate_output` therefore *extracts* JSON out of the text tolerantly:
it strips a Markdown code fence, tries a whole-text parse, then isolates the outermost
`{...}` / `[...]` span before decoding. If nothing parses, the single failure is
`NOT_JSON` and the raw text is returned unchanged as the value.

The inline-value contract — a plain typed value vs. an
[`ArtifactRef`](persistence.md) pointer for large blobs — is settled in
[ADR 0013](../architecture/decisions/0013-emission-taxonomy-and-inline-output-value.md): the value is inline by default, and validators
operate on that inline value. An `ArtifactRef` is an explicit opt-in dereferenced at a
single point, never something the validators chase.

### The two pass-through cases

`validate_output` does **not** always parse JSON. It returns `(text, [])` untouched in
two cases:

- **No declared outputs.** A run with an empty output schema keeps a plain-string value
  (back-compatibility with the era when every output was a string). There is nothing to
  validate, so this routine path raises no `EMPTY_SCHEMA` error.
- **A single `str` output.** A lone `str`-typed output is plain text, not JSON, so no
  parse is attempted and the raw text becomes the value.

`EMPTY_SCHEMA` exists in the `ValidationFailure` set for callers that treat a missing
schema as an error, but `validate_output`'s own no-schema path does not emit it.

### One output vs. many

After JSON is extracted, the shape rule depends on how many outputs are declared:

- **One declared output** *is* the whole value — a record, list, or primitive validated
  directly against that one type.
- **Multiple declared outputs** require the value to be a record (a JSON object) keyed by
  each output's `name`. A non-record value yields a single `TYPE_MISMATCH`; a required,
  defaultless output absent from the object yields `MISSING_FIELD` per missing key.

Type checks resolve through the structural [type system](type-system.md), not by string
equality. JSON's single number type means an `int` value satisfies a `float` field;
`bool` is rejected where `int`/`float` is wanted (a JSON `true` is not a number); a
`"json"`-typed field accepts anything; an unknown (nominal) primitive name accepts any
non-container scalar.

### Determinism via canonicalisation

Records are unordered, so before comparing or diffing, values are *canonicalised* —
every mapping's keys are recursively sorted; lists keep their order. This makes equality
and diffs reproducible under record/replay, so golden-set comparisons don't flap on key
ordering. `validate_output` canonicalises the parsed value; `structural_diff`
canonicalises both sides.

### `structural_diff` paths and the unused parameters

Field paths are dotted for records (`a.b`) and indexed for lists (`a[0]`). A list that
grew reports the new indices as `added`; one that shrank reports the dropped indices as
`removed`; a leaf whose value changed reports its path as `changed`. The `added` /
`removed` / `changed` tuples are sorted. `StructuralDiff.equal` is the convenience
predicate eval scoring keys off — `True` exactly when all three tuples are empty.

`structural_diff` accepts `schema` and `reg` keyword arguments, but they are reserved:
the diff is purely structural and ignores them. They exist for signature symmetry with
the other validators.

---

## API reference

### `ValidationFailure`

`class ValidationFailure(str, Enum)` — the closed set of structured failure *reasons*.

| Member | Value | Meaning |
| --- | --- | --- |
| `ValidationFailure.NOT_JSON` | `"not_json"` | Text was expected to be JSON and was not parseable. |
| `ValidationFailure.MISSING_FIELD` | `"missing_field"` | A required schema field was absent. |
| `ValidationFailure.TYPE_MISMATCH` | `"type_mismatch"` | A value's type was not registry-compatible. |
| `ValidationFailure.EXTRA_FIELD` | `"extra_field"` | A field not in the schema was present (strict mode). |
| `ValidationFailure.EMPTY_SCHEMA` | `"empty_schema"` | No output schema declared to validate against. |
| `ValidationFailure.CONSTRAINT` | `"constraint"` | A declared constraint (range/enum/etc.) was violated. |

### `ValidationAction`

`class ValidationAction(str, Enum)` — the *action* policy applied on failure, distinct
from the *reason*. The run executor reads this to decide what to do.

| Member | Value | Meaning |
| --- | --- | --- |
| `ValidationAction.RETRY` | `"retry"` | Re-run via the retry policy (transient failure). |
| `ValidationAction.REPAIR` | `"repair"` | Re-prompt the model with the schema error (one extra call). |
| `ValidationAction.DEAD_LETTER` | `"dead_letter"` | Give up and record the item for later replay. |

### `ValidationError`

`class ValidationError(BaseModel)` — one structured validation failure. **A frozen data
record, not a raised exception** (despite the `…Error` name). Returned in lists by the
validators.

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `failure` | `ValidationFailure` | — (required) | Which reason from the closed set. |
| `field` | `str \| None` | `None` | Dotted path to the offending field, if any. |
| `detail` | `str` | `""` | Human-readable explanation. Never contains secret values. |

### `StructuralDiff`

`class StructuralDiff(BaseModel)` — a typed, order-canonical difference between two
values. Frozen.

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `added` | `tuple[str, ...]` | `()` | Dotted field paths present only in *after*. |
| `removed` | `tuple[str, ...]` | `()` | Dotted field paths present only in *before*. |
| `changed` | `tuple[str, ...]` | `()` | Dotted field paths whose leaf value differs. |

`StructuralDiff.equal` is a read-only property: `True` when `added`, `removed`, and
`changed` are all empty.

### `validate_output`

```python
def validate_output(
    text: str,
    outputs: list[Parameter],
    reg: TypeRegistry | None = None,
) -> tuple[JSONValue, list[ValidationError]]
```

Parse and validate model `text` against the declared `outputs` schema. Returns
*(value, errors)* — the best-effort parsed, canonicalised value and a list of structured
failures (empty when valid). Pass-through `(text, [])` when there are no outputs or a
single `str` output; `(text, [NOT_JSON])` when JSON cannot be extracted. `reg` defaults
to `default_registry`.

### `validate_inputs`

```python
def validate_inputs(
    values: Mapping[str, JSONValue],
    schema: list[Parameter],
    reg: TypeRegistry | None = None,
) -> list[ValidationError]
```

Validate bound input `values` against the input `schema` — presence *and* type. A
missing required, defaultless input → `MISSING_FIELD`; a wrong-typed input →
`TYPE_MISMATCH`. Returns the (possibly empty) error list. `reg` defaults to
`default_registry`.

### `structural_diff`

```python
def structural_diff(
    before: JSONValue,
    after: JSONValue,
    *,
    schema: list[Parameter] | None = None,
    reg: TypeRegistry | None = None,
) -> StructuralDiff
```

Compute an order-canonical structural diff between two values. Both sides are
canonicalised (keys sorted) before comparison. `schema` and `reg` are reserved for
signature symmetry and ignored — the diff is purely structural.

---

## Example

Validate a conforming output, then a non-conforming one; check bound inputs; and diff
two records. Pure — no model call, no runtime.

```python
from crawfish.validation import (
    validate_output, validate_inputs, structural_diff, ValidationAction,
)
from crawfish.core.types import Parameter

# Two declared outputs → the value must be a JSON object keyed by name.
out = [Parameter(name="priority", type="str"), Parameter(name="score", type="int")]

# 1. Conforming model text (fenced JSON parses, both fields present and typed).
good = '```json\n{"priority": "high", "score": 3}\n```'
value, errors = validate_output(good, out)
print("good value:", value)
print("good errors:", errors)

# 2. Non-conforming: "priority" missing, "score" is a string not an int.
value2, errors2 = validate_output('{"score": "oops"}', out)
for e in errors2:
    print(f"  {e.failure.value} @ {e.field}: {e.detail}")

# 3. Inputs: a required input unbound, another bound to the wrong type.
schema = [Parameter(name="repo", type="str"), Parameter(name="limit", type="int")]
for e in validate_inputs({"limit": "ten"}, schema):
    print(f"  input {e.failure.value} @ {e.field}")

# 4. A structural diff: c added, b changed, a unchanged.
diff = structural_diff({"a": 1, "b": 2}, {"a": 1, "b": 9, "c": 3})
print("added:", diff.added, "changed:", diff.changed, "equal:", diff.equal)

# 5. The action policy is a separate enum.
print("actions:", [a.value for a in ValidationAction])
```

??? success "▶ Output"

    ```text
    good value: {'priority': 'high', 'score': 3}
    good errors: []
      missing_field @ priority: required output 'priority' is absent
      type_mismatch @ score: expected int, got str
      input missing_field @ repo
      input type_mismatch @ limit
    added: ('c',) changed: ('b',) equal: False
    actions: ['retry', 'repair', 'dead_letter']
    ```
