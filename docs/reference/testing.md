# Testing

The public test-harness toolkit that makes everything else testable: pin an output
to a file, run canned fixtures, replay a recorded model run instead of calling one
live, and assert that untrusted data stays flagged across every boundary. These live
in `crawfish.testing` and are what a future `craw test` command drives.

**Symbols on this page:** `snapshot_match` · `assert_snapshot` · `run_fixtures` ·
`assert_rubric` · `replaying` · `canned_transport` · `load_stream_fixture` ·
`injection_tool_result` · `scoring_runtime` · `TaintCase` · `taint_conformance_cases` ·
`assert_taint_conformance` · `INJECTION_INPUTS` · `STREAM_FIXTURES`

---

## Core

A test for an agent pipeline has one enemy: **non-determinism**. If a test calls a
live model, the answer changes run to run, the network can fail, and CI is unreliable.
Every helper here exists to remove that — tests run on canned data, recorded responses,
or pure checks, never a live call.

**Snapshot testing** pins a value to a file. The first run writes the file (the
*baseline*); later runs compare against it. If the value changes, the comparison fails
and you see exactly what differs. `snapshot_match` returns a true/false answer;
`assert_snapshot` raises instead, so it reads as a test failure.

A **fixture** is a small JSON file holding the inputs for one run and, optionally, the
output you expect. `run_fixtures` loads every fixture in a folder, runs the pipeline
once per fixture, and reports which passed.

A **Definition** is a saved pipeline; a **runtime** is the backend that actually runs an
agent. To keep that backend deterministic, you **replay**: instead of calling a model,
you read back a previously recorded response (a *cassette*). `replaying` wraps a runtime
so it does exactly that, and in the CI default a missing recording is an error rather
than a silent live call.

A **rubric** is a set of named scoring metrics over an output (quality, relevance, and
so on). `assert_rubric` turns a rubric into a pass/fail gate: each named metric must
score at or above a floor you give, or the test fails.

The remaining helpers support a shared determinism setup used across the framework:

- A **canned transport** (`canned_transport`) hands a backend a fixed block of recorded
  model output instead of launching `claude -p`. `load_stream_fixture` reads one such
  recording from disk; `STREAM_FIXTURES` is the folder they live in.
- **Prompt injection** is when untrusted text tries to act as instructions to the model
  ("ignore your previous instructions…"). `INJECTION_INPUTS` is a shared set of such
  hostile strings, and `injection_tool_result` returns one as a tool result, so security
  tests can prove the model treats them as data, never commands.
- `scoring_runtime` is a deterministic stand-in for an LLM-as-judge backend: it returns a
  fixed verdict with no model call.
- **Taint** is a flag meaning "this value was derived from something untrusted." The
  conformance helpers (`TaintCase`, `taint_conformance_cases`, `assert_taint_conformance`)
  check that the taint flag survives every step a value travels through.

---

## Ramps up

### Snapshots: write-on-missing, compare-after

`snapshot_match` serialises the value to **canonical JSON** — `json.dumps` with sorted
keys, two-space indent, and `default=str` for anything non-JSON. Two consequences follow:
key order in your value never matters, and the comparison is byte-stable across runs.

The write path fires when the snapshot file is missing **or** `update=True` is passed —
both write the file (creating parent directories) and return `True`. So the first run of
a new snapshot always passes; you commit the file and subsequent runs compare against it.
`update=True` is the accept-a-new-baseline escape hatch.

`assert_snapshot` calls `snapshot_match`; on a diff it builds a `difflib` unified diff
(expected snapshot vs actual) and raises `SnapshotMismatch` (a subclass of
`AssertionError`) carrying that diff.

### Fixtures run the pipeline once each, in sorted order

`run_fixtures` globs `*.json` in the directory and processes them in **sorted filename
order** for stable reporting. Each file is `{"inputs": {...}, "expected": <optional>}`.
For each, a fresh in-memory `RunContext` is built (SQLite-backed by default; override via
`ctx_factory`), the Definition runs once, and a `FixtureResult` is recorded.

A fixture **passes** when it executes cleanly and — only if `expected` is present — the
Output value equals it. A fixture with no `expected` passes on clean execution alone
(a smoke test). Every failure is caught and reported, never raised: a JSON load error, a
file read error, or any exception during the run becomes a `FixtureResult` with
`passed=False` and the message in `error`. `run_fixtures` is `async` — `await` it.

### Replay guarantees no live call

`replaying` wraps an inner runtime in a `RecordReplayRuntime` (see
[runtimes](runtimes.md)). With `record=False` — the CI default — a cache miss raises
`CassetteMiss`, so a test can never silently reach a live model. You set `record=True`
once to capture cassettes from the inner runtime, then commit them and run replay-only
forever after. Pair it with a `MockRuntime` or `scoring_runtime` as the inner backend to
capture a deterministic cassette.

### Canned transports vs cassettes

These are two layers. A **cassette** (`replaying`) records at the runtime level — whole
request/response pairs. A **canned transport** (`canned_transport`) records one layer
lower: it replaces the `Transport` a `CommandRuntime` uses to shell out to `claude -p`,
returning a fixed `stream-json` string verbatim. The returned coroutine ignores its
`(args, prompt)` arguments and always yields the same bytes, so a test asserts on the
parsed `RunResult` with no subprocess and no model. `load_stream_fixture` reads one of
these recordings — a provider stem like `"anthropic_clean"` (no `.jsonl` suffix) — from
`STREAM_FIXTURES`, the `tests/fixtures/streams/` folder. The shipped provider stems are
`anthropic`, `openai`, `gemini`, and `local`; an `*_injection` variant carries an
untrusted tool result that attempts prompt injection.

### The static / fluid boundary and taint

Crawfish marks every input as **static** (set once per batch, trusted) or **fluid**
(varies per item, untrusted — see [core types](core-types.md)). Untrusted data must never
silently become trusted. Two mechanisms in this module test that:

- `INJECTION_INPUTS` / `injection_tool_result` supply the hostile probes. They are *data*
  the model reads, never instructions — security suites bind them as fluid input and a
  tool result and assert the value never crosses into the instruction half of the prompt.
- **Taint** is the flag that records "this came from something untrusted." The conformance
  suite asserts taint propagates across every Phase-2 boundary: the `Output.derive` step,
  the `Emission` that carries a value, and the transferable `Context` artifact (taint must
  survive compaction — a summary of a tainted entry stays tainted). The load-bearing rule:
  a value that came back through a **tool / MCP result** is tainted regardless of whether
  the originating input was static — tool results re-enter the model as untrusted content.
  See [sandbox and jail](sandbox-and-jail.md) for how taint is enforced at runtime.

The conformance matrix has exactly one clean row — static input, no tool result — because
that is the only path where nothing untrusted ever touched the value.

---

## API reference

### `snapshot_match`

```python
def snapshot_match(path: str | Path, value: JSONValue, *, update: bool = False) -> bool
```

Compare `value` (as canonical JSON) against the snapshot at `path`. Writes the file and
returns `True` when it is **missing** or `update=True`. Otherwise returns `True` on a
match, `False` on a diff. Never raises on a diff — the caller decides how to surface it.

### `assert_snapshot`

```python
def assert_snapshot(path: str | Path, value: JSONValue, *, update: bool = False) -> None
```

Like `snapshot_match`, but raises `SnapshotMismatch` (subclass of `AssertionError`) on a
diff, with a unified diff (snapshot vs actual) in the message. Returns `None` on match or
write.

### `run_fixtures`

```python
async def run_fixtures(
    fixtures_dir: str | Path,
    definition: Definition,
    runtime: AgentRuntime,
    *,
    ctx_factory: Callable[[], RunContext] | None = None,
) -> list[FixtureResult]
```

Run every `*.json` fixture in `fixtures_dir` (sorted by filename) against `definition`
using `runtime`. Each fixture is `{"inputs": {...}, "expected": <optional>}`. Returns one
`FixtureResult` per fixture. `ctx_factory` is an optional zero-arg callable returning a
fresh `RunContext` per fixture; defaults to an in-memory SQLite-backed context.

**`FixtureResult`** (`@dataclass`) — the outcome of one fixture:

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `name` | `str` | — (required) | Fixture filename stem. |
| `passed` | `bool` | — (required) | Ran cleanly and (if given) matched `expected`. |
| `inputs` | `dict[str, JSONValue]` | `{}` | Inputs the fixture supplied. |
| `expected` | `JSONValue \| None` | `None` | Expected Output value, if the fixture declared one. |
| `actual` | `JSONValue \| None` | `None` | The Output value produced. |
| `error` | `str \| None` | `None` | Failure message on load/run error, else `None`. |

### `assert_rubric`

```python
def assert_rubric(
    output: Output[JSONValue],
    rubric: Rubric,
    thresholds: dict[str, float],
) -> None
```

Score `output` with `rubric`, then assert each metric named in `thresholds` scores `>=`
its floor. Raises `RubricThresholdError` (subclass of `AssertionError`) listing every
metric that fell short — and every threshold naming a metric absent from the rubric.
Returns `None` when all clear.

### `replaying`

```python
def replaying(
    inner_runtime: AgentRuntime,
    cassette_dir: str | Path,
    *,
    record: bool = False,
) -> RecordReplayRuntime
```

Wrap `inner_runtime` in a [`RecordReplayRuntime`](runtimes.md). `record=False` (the CI
default) replays cassettes from `cassette_dir`; a miss raises `CassetteMiss`, guaranteeing
no live call. `record=True` captures cassettes from `inner_runtime`.

### `canned_transport`

```python
def canned_transport(stream: str) -> Transport
```

Return a `Transport` (an `async (args, prompt) -> str` coroutine) that ignores its
arguments and always yields `stream` verbatim. Inject it into a `CommandRuntime` so a run
parses canned `stream-json` with no subprocess and no live call.

### `load_stream_fixture`

```python
def load_stream_fixture(name: str, *, fixtures_dir: str | Path | None = None) -> str
```

Read a canned `stream-json` fixture's text by stem (e.g. `"anthropic_clean"`, no `.jsonl`
suffix). Reads from `fixtures_dir` if given, else from `STREAM_FIXTURES`. Pair with
`canned_transport` to drive a `CommandRuntime` deterministically.

### `injection_tool_result`

```python
def injection_tool_result(name: str = "ignore_instructions") -> str
```

Return one entry of `INJECTION_INPUTS` as a flat string — an untrusted tool/MCP result
that attempts prompt injection. Default key is `"ignore_instructions"`. A value derived
from this must be tainted (the "static input + tool result → tainted" conformance case).

### `scoring_runtime`

```python
def scoring_runtime(score: float = 1.0, *, verdict: str | None = None) -> MockRuntime
```

Return a deterministic LLM-judge / tuner backend: a [`MockRuntime`](runtimes.md) whose
responder always returns the same text, no model call. `score` is clamped to `[0, 1]` and
embedded in the verdict (`"score: <s> — meets the criteria"`); pass an explicit `verdict`
to override the text. Wrap in `replaying(..., record=True)` once to capture a real
cassette.

### `TaintCase`

`@dataclass(frozen=True)` — one row of the taint-propagation conformance matrix.

| Field | Type | Notes |
| --- | --- | --- |
| `name` | `str` | Case label. |
| `source_tainted` | `bool` | Taint of the originating input (a fluid input is tainted; static-only is not). |
| `from_tool` | `bool` | The value came back through a tool/MCP result (untrusted regardless of input flow). |
| `expected` | `bool` | Whether the derived Output **and** its Emission must end up tainted. |

### `taint_conformance_cases`

```python
def taint_conformance_cases() -> tuple[TaintCase, ...]
```

Return the reusable taint matrix — four `TaintCase`s asserted across every boundary:

| `name` | `source_tainted` | `from_tool` | `expected` |
| --- | --- | --- | --- |
| `fluid_input` | `True` | `False` | `True` |
| `static_plus_tool` | `False` | `True` | `True` |
| `fluid_plus_tool` | `True` | `True` | `True` |
| `static_no_tool` | `False` | `False` | `False` |

### `assert_taint_conformance`

```python
def assert_taint_conformance(cases: Sequence[TaintCase] | None = None) -> None
```

Assert taint propagates across every Phase-2 boundary for each case (defaults to
`taint_conformance_cases()`). For each case it derives an `Output` via `Output.derive`,
builds the matching `Emission`, and carries the value through the transferable `Context`
artifact and a `CarrySummary` compaction — asserting all of them carry the expected
taint, including the invariant that a tool-derived `Emission` has kind `TOOL` and is
`tainted=True`. Raises `AssertionError` on the first violation; returns `None` if all
pass.

### `INJECTION_INPUTS`

`INJECTION_INPUTS: dict[str, JSONValue]` — a shared set of fluid (untrusted) strings that
attempt prompt injection. Keys: `ignore_instructions`, `exfiltrate_secret`,
`role_override`, `tool_redirect`. Each value is a hostile instruction-like string treated
purely as data.

### `STREAM_FIXTURES`

`STREAM_FIXTURES: Path` — the `tests/fixtures/streams/` directory holding the canned
per-provider `*.jsonl` `stream-json` recordings. Imported rather than hard-coded by tests
that load a stream fixture.

---

## Example

Snapshot testing a value (match then mismatch), and reading the shipped taint matrix —
all pure and in-memory, no runtime needed.

```python
import tempfile, os
from crawfish.testing import snapshot_match, taint_conformance_cases

d = tempfile.mkdtemp()
snap = os.path.join(d, "report.json")

# First call writes the baseline (file missing -> True).
print(snapshot_match(snap, {"status": "ok", "count": 3}))
# Same value, different key order, matches the written baseline.
print(snapshot_match(snap, {"count": 3, "status": "ok"}))
# A changed value diverges.
print(snapshot_match(snap, {"status": "ok", "count": 4}))

# How many taint conformance cases ship, and each expected verdict.
cases = taint_conformance_cases()
print(len(cases))
for c in cases:
    print(c.name, c.expected)
```

??? success "▶ Output"

    ```text
    True
    True
    False
    4
    fluid_input True
    static_plus_tool True
    fluid_plus_tool True
    static_no_tool False
    ```
