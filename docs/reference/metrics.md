# Metrics

How agent quality becomes a number you can compare across versions. A metric scores
one output to a float; rubrics bundle metrics into a score vector; benchmarks run a
rubric over a task set and aggregate. These live in `crawfish.metrics` and feed the
[evaluation harness](evals.md) — metrics are the graders evals run.

**Symbols on this page:** `Metric` · `Rubric` · `Benchmark` · `output_number` ·
`field_present` · `is_nonempty` · `confidence_threshold` · `FieldExactMatch` ·
`SetOverlap` · `NumericTolerance` · `SchemaConformance` · `StructuralMatch` ·
`field_exact_match` · `set_overlap` · `numeric_tolerance` · `schema_conformance` ·
`structural_match` · `compare` · `is_regression`

---

## Core

An **output** is what an agent produced for one task — a number, a string, or a typed
record (a `dict`). A **metric** looks at one output and returns a single `float`: a
**score**. The convention is *higher is better*; pass/fail metrics use `1.0` for pass
and `0.0` for fail.

There are two families of metric:

- **Simple checks** read the raw output and ask one yes/no-ish question — is there a
  number in it (`output_number`), is a field present (`field_present`), is it non-empty
  (`is_nonempty`), does a confidence field clear a bar (`confidence_threshold`).
- **Comparators** score the output *against an expected answer* you supply — does a
  field equal it exactly (`field_exact_match`), how much do two lists overlap
  (`set_overlap`), is a number close enough (`numeric_tolerance`), does it match a
  declared shape (`schema_conformance`), how close is the whole record
  (`structural_match`).

A **rubric** is a named collection of metrics scored together. Its `score` returns a
**vector** — a `dict` mapping each metric's `name` to its float. A **benchmark** runs a
rubric over a fixed set of tasks, scores every result, and averages each metric across
tasks into one comparable vector.

That comparable vector is the point. Run an old agent version and a new one over the
same tasks, then `compare` the two vectors to get per-metric deltas, and `is_regression`
tells you whether the new version got worse on anything. This is the **improvement
loop**: change the agent, re-score, ship only if nothing regressed.

Every example here stays deterministic by scoring outputs directly — no model call, so
scores never drift.

---

## Ramps up

### Class and factory come in pairs

Each comparator is a `Metric` subclass with a matching lowercase **factory function**:
`FieldExactMatch` / `field_exact_match`, `SetOverlap` / `set_overlap`,
`NumericTolerance` / `numeric_tolerance`, `SchemaConformance` / `schema_conformance`,
`StructuralMatch` / `structural_match`. The simple checks follow the same pattern:
`OutputNumber` / `output_number`, `FieldPresent` / `field_present`, `IsNonempty` /
`is_nonempty`, `ConfidenceThreshold` / `confidence_threshold`.

The factory is the ergonomic front door — it forwards to the class constructor with the
common arguments and nothing more. The classes accept one extra keyword the factories
don't: `name`, to override the auto-generated metric name (which is what keys the rubric
vector). Reach for the class when you need a custom name or a metric mode the factory
doesn't surface; otherwise use the factory.

### Reading the typed value

Comparators read the **typed** output value. When an agent's `Definition` declares a
record or list output schema, `Output.value` already holds a real `dict`/`list` and the
comparator reads it directly. For back-compat, an output with no declared schema holds a
plain string; a comparator decodes it **only if** the string is exactly one
self-contained JSON document. Two concatenated objects (`{"a":1}{"b":2}`) decode to
nothing rather than silently scoring the first — the metric refuses to guess. Records
are canonicalised (keys sorted) before comparison, so `{"a":1,"b":2}` and
`{"b":2,"a":1}` score as equal.

A `field` argument (a dotted path like `"summary.label"`) restricts a comparator to one
subtree; `field=None` (the default) compares the whole value. An absent path resolves to
`None`.

### How each comparator scores

Every comparator returns a float in `[0, 1]`. The scoring rule differs:

- **`FieldExactMatch`** — `1.0` if the (canonicalised) field equals `expected`, else
  `0.0`. Binary.
- **`SetOverlap`** — order-free overlap of a list/set field against expected members.
  `mode="f1"` (default) scores the harmonic mean of precision and recall;
  `mode="jaccard"` scores intersection / union. Two empty sets score `1.0`; no overlap
  scores `0.0`.
- **`NumericTolerance`** — `1.0` if a numeric field is within `tol` of `expected`, else
  `0.0`. `relative=True` makes `tol` a fraction of `|expected|`. A non-numeric or absent
  value (including a `bool`, which is *not* treated as numeric) scores `0.0`.
- **`SchemaConformance`** — `1 - errors/checks`, where `checks` is the number of declared
  leaf fields. A 2-field record missing one field scores `0.5`. A clean parse with no
  errors is `1.0`; an unparseable payload is `0.0` outright (not "one error of N").
- **`StructuralMatch`** — `1.0` when the semantic diff against `expected` is empty;
  otherwise `1 - changes/total_paths`, so a value differing in one of ten fields scores
  `0.9`.

### Comparing two score vectors

`compare(a, b)` returns per-metric deltas `b - a` (read as *candidate minus baseline*):
positive means the candidate improved, negative means it dropped. Metrics present on
only one side are treated as `0.0` on the other, so the two vectors need not have the
same keys.

`is_regression(baseline, candidate)` returns `True` if any metric's delta falls below
`-tolerance`. The default `tolerance=0.0` flags any drop at all; a small tolerance
absorbs scoring noise. Higher-is-better is assumed for every metric — a metric where
lower is better must be inverted before it reaches these functions.

### Determinism by construction

A `Benchmark` drives each task through a real `Run`, so it needs an `AgentRuntime`.
Pair it with `MockRuntime` (see the [evals reference](evals.md)) and the whole loop is
deterministic — no live model call, so iterating on metrics never burns budget and
scores never shift between runs.

---

## API reference

### `Metric`

`class Metric(ABC)` — a single scalar quality signal over one output.

| Member | Type | Notes |
| --- | --- | --- |
| `name` | `str` | Keys this metric in a `Rubric` score vector. |
| `evaluate(output)` | `(Output[JSONValue]) -> float` | Abstract. Scores the output; higher is better, `1.0`/`0.0` for pass/fail. |

### Simple checks

Each is a `Metric` subclass paired with a factory. The factory takes the common
arguments; the class additionally takes `name` to override the auto-generated metric
name.

#### `output_number`

```python
def output_number(*, field: str | None = None, default: float = 0.0) -> OutputNumber
```

Extracts a numeric from the output. If the value is itself a number it is returned; a
mapping is probed by `field`; otherwise the first numeric token in the string form is
used. `default` is returned when nothing numeric is found. (A `bool` is read as `1.0`/`0.0`.)
Class: `OutputNumber(*, field=None, default=0.0, name=None)`.

#### `field_present`

```python
def field_present(field: str) -> FieldPresent
```

`1.0` if `field` is present and non-null in the output value, else `0.0`.
Class: `FieldPresent(field, *, name=None)`.

#### `is_nonempty`

```python
def is_nonempty(*, field: str | None = None) -> IsNonempty
```

`1.0` if the output value (or `field`, when given) is non-empty, else `0.0`. Empty means:
empty/whitespace string, empty collection, or `None`.
Class: `IsNonempty(*, field=None, name=None)`.

#### `confidence_threshold`

```python
def confidence_threshold(field: str, threshold: float) -> ConfidenceThreshold
```

`1.0` if `field`'s numeric confidence is `>= threshold`, else `0.0`.
Class: `ConfidenceThreshold(field, threshold, *, name=None)`.

### Comparators

Each is a `Metric` subclass paired with a factory. They read the typed output value (see
[Reading the typed value](#reading-the-typed-value)).

#### `field_exact_match`

```python
def field_exact_match(expected: JSONValue, *, field: str | None = None) -> FieldExactMatch
```

`1.0` if the field (canonicalised) equals `expected`, else `0.0`.
Class: `FieldExactMatch(expected, *, field=None, name=None)`.

#### `set_overlap`

```python
def set_overlap(
    expected: JSONValue, *, field: str | None = None, mode: str = "f1"
) -> SetOverlap
```

Order-free overlap of a list/set field against `expected`. `mode="f1"` scores the
harmonic mean of precision and recall; `mode="jaccard"` scores intersection / union. Two
empty sets score `1.0`. The class raises `ValueError` if `mode` is neither `"f1"` nor
`"jaccard"`.
Class: `SetOverlap(expected, *, field=None, mode="f1", name=None)`.

#### `numeric_tolerance`

```python
def numeric_tolerance(
    expected: float, *, field: str | None = None, tol: float = 1e-9, relative: bool = False
) -> NumericTolerance
```

`1.0` if a numeric field is within `tol` of `expected`, else `0.0`. `relative=True` makes
`tol` a fraction of `|expected|` (with an absolute floor when `expected == 0`).
Non-numeric or absent values (a `bool` included) score `0.0`.
Class: `NumericTolerance(expected, *, field=None, tol=1e-9, relative=False, name=None)`.

#### `schema_conformance`

```python
def schema_conformance(schema: list[Parameter]) -> SchemaConformance
```

Fraction in `[0, 1]` of declared-schema checks the typed value passes: `1 - errors/checks`,
where `checks` is the number of declared leaf fields. A clean parse is `1.0`; an
unparseable payload is `0.0`.
Class: `SchemaConformance(schema, *, name=None)`.

#### `structural_match`

```python
def structural_match(expected: JSONValue, *, field: str | None = None) -> StructuralMatch
```

Semantic-diff score against `expected`: `1.0` when the diff is empty, else
`1 - changes/total_paths`.
Class: `StructuralMatch(expected, *, field=None, name=None)`.

### `Rubric`

`class Rubric` — a named collection of metrics scored together.

```python
Rubric(metrics: Sequence[Metric], *, name: str = "rubric")
def score(self, output: Output[JSONValue]) -> dict[str, float]
```

`score` runs every metric and returns `{metric.name: float}`.

### `Benchmark`

`class Benchmark` — a rubric run over a fixed task set, aggregated to comparable scores.

```python
Benchmark(
    rubric: Rubric,
    tasks: Sequence[Task],
    *,
    name: str = "benchmark",
    inputs_for: Callable[[Task], dict[str, JSONValue]] | None = None,
)
async def run(
    self, definition: Definition, ctx: RunContext, runtime: AgentRuntime
) -> dict[str, float]
```

`run` executes `definition` on every task (one `Run` per task, binding the task as the
fluid `task`/`task_id` inputs unless `inputs_for` overrides), scores each Output with the
rubric, and returns the per-metric **mean** across tasks. An empty task set returns
`0.0` for every metric. `inputs_for` defaults to `{"task": task.description, "task_id":
task.id}`.

### `compare`

```python
def compare(scores_a: dict[str, float], scores_b: dict[str, float]) -> dict[str, float]
```

Per-metric deltas `b - a` (candidate minus baseline). Positive is an improvement,
negative a drop. Metrics absent from a side are treated as `0.0`, so vectors need not
align.

### `is_regression`

```python
def is_regression(
    baseline: dict[str, float],
    candidate: dict[str, float],
    *,
    tolerance: float = 0.0,
) -> bool
```

`True` if `candidate` is worse than `baseline` on any metric — i.e. some delta drops
below `-tolerance`. Higher-is-better is assumed for every metric.

---

## Example

Score one output with three comparators, then run `compare` + `is_regression` on a
baseline and a candidate score vector. Pure and in-memory — no runtime needed.

```python
from crawfish.metrics import (
    field_exact_match,
    set_overlap,
    numeric_tolerance,
    Rubric,
    compare,
    is_regression,
)
from crawfish.output import Output

# A typed output: an agent's structured verdict on a ticket.
out = Output(
    produced_by="triage-agent",
    value={
        "label": "bug",
        "tags": ["crash", "ui"],   # expected also wants "ux" -> partial overlap
        "confidence": 0.82,
    },
)

rubric = Rubric([
    field_exact_match("bug", field="label"),
    set_overlap(["crash", "ui", "ux"], field="tags"),         # F1 over 2-of-3
    numeric_tolerance(0.80, field="confidence", tol=0.05),    # 0.82 within 0.05
])
scores = rubric.score(out)
for name, value in scores.items():
    print(f"{name}: {round(value, 3)}")

# Improvement loop: baseline vs candidate score vectors over the same metrics.
baseline  = {"accuracy": 0.90, "coverage": 0.70}
candidate = {"accuracy": 0.92, "coverage": 0.55}   # coverage dropped
delta = compare(baseline, candidate)
for name in sorted(delta):                          # sort: dict order is hash-seeded
    print(f"delta {name}: {round(delta[name], 3)}")
print("regressed (tol=0.0):", is_regression(baseline, candidate))
print("regressed (tol=0.2):", is_regression(baseline, candidate, tolerance=0.2))
```

??? success "▶ Output"

    ```text
    field_exact_match[label]: 1.0
    set_overlap.f1[tags]: 0.8
    numeric_tolerance[confidence]: 1.0
    delta accuracy: 0.02
    delta coverage: -0.15
    regressed (tol=0.0): True
    regressed (tol=0.2): False
    ```
