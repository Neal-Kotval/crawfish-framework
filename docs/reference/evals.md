# Evals

The eval *data* lifecycle: capture real runs as reusable examples, curate them
into versioned collections, grade outputs, and gate a new version against a stored
baseline so quality can't silently regress. The scoring *types* (`Metric`, `Rubric`)
live in [metrics](metrics.md); this page is everything that turns runs into a
durable, gradable corpus. These live in `crawfish.eval`.

**Symbols on this page:** `EvalCase` · `GoldenSet` · `LLMJudge` · `capture_case` ·
`grade_output` · `save_baseline` · `load_baseline` · `gate_against_baseline` ·
`upconvert_case` · `migrate_golden_set`

---

## Core

An **eval case** is one worked example: the **inputs** that went into a run, the
**output** it produced, and an optional **label** — a human's judgment of what the
right answer was. `EvalCase` is the record that holds all three. You build one by
hand, or you capture it from a real run with `capture_case`.

A **golden set** is a named, versioned collection of those cases — your reference
corpus of "here are inputs and here is what good looks like." `GoldenSet` stores its
cases through the [`Store`](persistence.md) (the persistence seam), so the corpus survives
across runs and machines. *Versioned* means the same name can hold several
generations (`triage@0.1`, `triage@0.2`); each lives under its own key.

**Grading** scores an output. There are two kinds of grader:

- A **rubric** — a bundle of coded [metrics](metrics.md), pure functions that read
  the output and return a number (is this field present? does it equal the expected
  value?). Deterministic, no model.
- An **LLM judge** — a model that reads the output and scores it against criteria,
  for qualities no coded check captures ("is this summary faithful?"). `LLMJudge`
  wraps that. `grade_output` runs both kinds and merges their scores into one
  `{name: score}` dictionary.

A **baseline** is a saved score dictionary — the quality bar a known-good version
cleared. `save_baseline` writes it; `load_baseline` reads it back. The payoff is the
**eval gate**: `gate_against_baseline` scores a candidate, compares it to the stored
baseline, and returns `True` only if no metric got worse. Wire that into CI and a
change that degrades quality fails the build.

`upconvert_case` and `migrate_golden_set` handle an older storage format —
explained under [Ramps up](#schema-migration-the-string-era).

---

## Ramps up

### Grading: coded metrics vs. the LLM judge

The two graders are complementary, not redundant. Coded metrics (a `Rubric`) are
cheap, deterministic, and catch structural failures — a missing field, a wrong enum,
a confidence below threshold. An `LLMJudge` catches the qualities a coded check can't
articulate: faithfulness, tone, whether an answer actually addresses the question.

`grade_output` is the single front door over both. Pass it a `rubric`, a list of
`judges`, or both; it returns the union of their scores in one flat dictionary keyed
by each metric's or judge's `name`. A judge's score keys under `LLMJudge.name`
(default `"llm_judge"`), so two judges must carry distinct names to avoid clobbering.

The judge feeds the candidate output to the grading agent as **fluid** input —
untrusted session data that reaches the model as data to read, never as instructions
to obey (the [security spine](../architecture/SECURITY.md)). The agent's free-text
verdict is parsed for the first number and clamped to `[0, 1]`; a verdict with no
number scores `0.0`. Under a `MockRuntime` (or record/replay) the judge is fully
deterministic, which is what keeps eval suites reproducible.

### The eval gate

`gate_against_baseline` is the regression check. It loads the stored baseline,
compares it to the candidate score-by-score, and returns `True` (pass) unless some
metric dropped. The comparison is [`is_regression`](metrics.md) from the metrics
module: a metric regresses when `candidate - baseline` falls below `-tolerance`.
Every metric is assumed **higher-is-better**.

Two edge cases matter:

- **No baseline yet.** If `load_baseline` returns `None`, the gate returns `True` —
  there is nothing to regress against. The first run establishes the bar; it never
  blocks.
- **`tolerance`.** A small `tolerance` (default `0.0`) absorbs scoring noise so a
  trivial dip doesn't fail the gate. At `0.0`, *any* drop on *any* metric fails.

Metrics present on only one side are treated as `0.0` on the other, so the baseline
and candidate vectors need not have identical keys.

### Schema migration: the string era

Golden sets captured before the typed-output change stored `output` and `label` as
JSON-encoded **strings**; metrics now read the **typed** value. `upconvert_case`
bridges the two: given a stored row, it lifts any `output`/`label` field that holds a
single self-contained JSON document back to its typed form, canonicalising it so the
result is reproducible under record/replay. It is **pure and idempotent** — an
already-typed row up-converts to itself, so it is safe to apply on every read. A
string that is *not* a single JSON document (free text, or two concatenated objects)
is left untouched; the converter never guesses.

Because a golden set's storage `kind` is dynamic (`golden:NAME@VERSION`), there is no
static converter table to register against. Instead the lift is applied lazily on the
read path: `GoldenSet.get` and `GoldenSet.cases` run every row through
`upconvert_case` before validating it. To rewrite the stored rows in place — a
one-time bulk lift — call `GoldenSet.migrate`, or the module-level
`migrate_golden_set` convenience wrapper; both return the count of cases that
actually changed.

---

## API reference

### `EvalCase`

`class EvalCase(BaseModel)` — one captured example: inputs, produced output, optional
human label.

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `id` | `str` | `new_id()` | Fresh UUID4 per case. |
| `inputs` | `dict[str, JSONValue]` | `{}` | The inputs that drove the run. |
| `output` | `JSONValue` | `None` | The produced output value. |
| `produced_by` | `str \| None` | `None` | Node id that emitted the output. |
| `transcript` | `list[JSONValue]` | `[]` | Optional run transcript. |
| `label` | `JSONValue` | `None` | Human judgment / expected output. |
| `metadata` | `dict[str, JSONValue]` | `{}` | Free-form annotations. |

### `GoldenSet`

`class GoldenSet` — a named, versioned collection of cases, persisted through a
`Store`.

```python
GoldenSet(store: Store, name: str, *, org_id: str = "local", version: str = "0.1")
```

Cases are stored under the `kind` `f"golden:{name}@{version}"`.

| Method | Signature | Behaviour |
| --- | --- | --- |
| `add` | `add(case: EvalCase) -> None` | Persist a case (keyed by `case.id`). |
| `label` | `label(case_id: str, label: JSONValue) -> None` | Attach/replace a case's label. Raises `KeyError` if the case is absent. |
| `get` | `get(case_id: str) -> EvalCase \| None` | Read one case (up-converted), or `None`. |
| `cases` | `cases() -> list[EvalCase]` | All cases in the set (each up-converted). |
| `migrate` | `migrate() -> int` | Rewrite every stored case through `upconvert_case`; returns the count that changed. Idempotent. |

### `capture_case`

```python
def capture_case(
    *,
    inputs: dict[str, JSONValue],
    output: Output[JSONValue],
    transcript: list[JSONValue] | None = None,
    label: JSONValue = None,
) -> EvalCase
```

Build an `EvalCase` from a real run. Copies `output.value` into `EvalCase.output` and
`output.produced_by` into `EvalCase.produced_by`. Keyword-only.

### `LLMJudge`

`class LLMJudge` — a `Definition`-backed grader: an agent scores an output against
criteria. Complements coded `Metric`s; deterministic under a mock/replay runtime.

```python
LLMJudge(definition: Definition, runtime: AgentRuntime, *, name: str = "llm_judge")

async def grade(
    self, output: Output[JSONValue], ctx: RunContext, *, criteria: str = "quality"
) -> float
```

`grade` binds `output.value` and `criteria` as fluid inputs, runs the judging agent
team, and parses its free-text verdict into a clamped `[0, 1]` score (no number →
`0.0`).

### `grade_output`

```python
async def grade_output(
    output: Output[JSONValue],
    ctx: RunContext,
    *,
    rubric: Rubric | None = None,
    judges: list[LLMJudge] | None = None,
) -> dict[str, float]
```

Score `output` with a coded `rubric` and/or a list of `judges`, returning the merged
`{name: score}` dictionary. A judge's score keys under its `name`.

### `save_baseline`

```python
def save_baseline(
    store: Store, name: str, scores: dict[str, float], *, org_id: str = "local"
) -> None
```

Persist `scores` as the baseline named `name` (under `kind` `"eval_baseline"`).

### `load_baseline`

```python
def load_baseline(
    store: Store, name: str, *, org_id: str = "local"
) -> dict[str, float] | None
```

Read the baseline named `name` back, with values coerced to `float`, or `None` if
none was saved.

### `gate_against_baseline`

```python
def gate_against_baseline(
    store: Store,
    name: str,
    candidate: dict[str, float],
    *,
    tolerance: float = 0.0,
    org_id: str = "local",
) -> bool
```

`True` if `candidate` passes — no metric regressed beyond `tolerance` versus the
stored baseline. Returns `True` when no baseline exists yet. Higher-is-better is
assumed for every metric. See [`is_regression`](metrics.md) for the comparison rule.

### `upconvert_case`

```python
def upconvert_case(rec: dict[str, JSONValue]) -> dict[str, JSONValue]
```

Up-convert a stored `EvalCase` row from the string era: lifts each of `output` and
`label` (when present) from a single JSON-encoded string to its typed, canonicalised
value. Pure, deterministic, idempotent; already-typed rows pass through unchanged.
Strings that aren't a single self-contained JSON document are left as-is.

### `migrate_golden_set`

```python
def migrate_golden_set(
    store: Store, name: str, *, version: str = "0.1", org_id: str = "local"
) -> int
```

Bulk-migrate a named/versioned golden set's cases to typed values in place. A
convenience wrapper over `GoldenSet.migrate`; returns the number of cases rewritten.

---

## Example

Build a small golden set, grade outputs with a coded (non-model) rubric, save a
baseline, then gate a matching candidate (pass) and a regressed one (fail). Fully
deterministic — no runtime, no model.

```python
from crawfish.store.sqlite import SqliteStore
from crawfish.output import Output
from crawfish.metrics import Rubric, FieldPresent, FieldExactMatch
from crawfish.eval import (
    EvalCase, GoldenSet, capture_case,
    save_baseline, load_baseline, gate_against_baseline,
)

store = SqliteStore(":memory:")

# A golden set is a named, versioned collection of cases.
golden = GoldenSet(store, "triage", version="0.1")
golden.add(EvalCase(inputs={"ticket": "disk full"},
                    output={"team": "infra", "priority": "high"}))
golden.add(EvalCase(inputs={"ticket": "typo in docs"},
                    output={"team": "docs", "priority": "low"}))
print("cases:", len(golden.cases()))

# Capture a fresh run as a case (no model — a plain Output).
out = Output(value={"team": "infra", "priority": "high"}, produced_by="router-1")
case = capture_case(inputs={"ticket": "disk full"}, output=out, label={"team": "infra"})
print("produced_by:", case.produced_by, "| label:", case.label)

# Grade an output with a NON-model rubric (deterministic, no runtime).
rubric = Rubric([FieldPresent("team"),
                 FieldExactMatch("infra", field="team")])
good = Output(value={"team": "infra", "priority": "high"}, produced_by="v2")
scores = rubric.score(good)
print("scores:", scores)

# Save a baseline, then gate candidates against it.
save_baseline(store, "triage", scores)
print("baseline:", load_baseline(store, "triage"))

# Same scores -> passes the gate.
print("pass:", gate_against_baseline(store, "triage", scores))

# A regression (team field now wrong) -> fails the gate.
bad = Output(value={"team": "WRONG", "priority": "high"}, produced_by="v3")
print("fail:", gate_against_baseline(store, "triage", rubric.score(bad)))
```

??? success "▶ Output"

    ```text
    cases: 2
    produced_by: router-1 | label: {'team': 'infra'}
    scores: {'field_present[team]': 1.0, 'field_exact_match[team]': 1.0}
    baseline: {'field_present[team]': 1.0, 'field_exact_match[team]': 1.0}
    pass: True
    fail: False
    ```

Grading and gating are the close of the quality loop: capture runs into a golden set,
score them, and let `gate_against_baseline` block any version that drops below the
bar. To improve a Definition *toward* that bar rather than merely guard it, see
[the tuner and learning loop](tuner-and-learning.md).
