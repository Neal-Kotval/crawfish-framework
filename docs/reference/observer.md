# Observer

The watchdog tier: a primitive that polls a pipeline's recent runs and raises
structured findings when something looks wrong, plus the read-only surface those
findings (and the run summaries they judge) live on. The rules engine is
`crawfish.observer`; the surface/facade is `crawfish.observe`.

**Symbols on this page:** `Observer` · `ObserverContext` · `Rule` · `FailureRateAbove` ·
`CostSpike` · `StuckRun` · `ObserverEvent` · `ObserverSurface` · `RunInfo` · `Severity` ·
`parse_since`

---

## Core

A **pipeline** is a chain of steps that runs over your data; each execution of one is a
**run**. As runs pile up you want to know — without watching a terminal — when failures
spike, when spend jumps, or when a run hangs. That is an **observer's** job.

An `Observer` watches one named pipeline. On a poll interval it reads the pipeline's
recent runs and asks each of its **rules** "does this look wrong?". A `Rule` is a cheap,
deterministic check — no model call, just arithmetic over the run list. When a rule trips
it returns an `ObserverEvent`: a structured finding with a `kind` (e.g. `"cost.spike"`),
a human-readable `detail`, and a `severity`. The three built-in rules are:

- `FailureRateAbove` — too large a fraction of recent runs failed.
- `CostSpike` — total spend in a recent window crossed a dollar threshold.
- `StuckRun` — a run has been `running` longer than allowed.

The observer can also carry an optional **judge**: a model-backed reviewer that reads
recent runs as *data* (never as instructions — the prompt-injection boundary) and reports
run quality in plain English, under a hard per-evaluation cost cap.

Where do the runs come from, and where do findings go? Both live on the **observer
surface**, `ObserverSurface` — a read/write **facade** (a thin, stable API) over the
framework's `Store` (its persistence layer). The surface holds two shapes:

- `RunInfo` — a per-run summary row: status, cost, timing. This is what a dashboard or
  `craw manage` renders.
- `ObserverEvent` — an append-only finding. Emitted findings land here in order.

Because the surface goes through the `Store` and never writes raw SQL of its own, wrapping
the store in a redacting `ScrubbingStore` automatically scrubs secrets and PII *before*
they are written — the security guarantee for this surface.

`Severity` labels how loudly a finding should be surfaced (`INFO` / `WARN` / `CRITICAL`).
`parse_since` is the small helper that turns a window like `"-1h"` into an epoch-seconds
cutoff, so every "recent runs" query agrees on what *recent* means.

> The Observer **reports**; it does not act. For the related engine that *auto-halts* a
> misbehaving pipeline, see [anomaly](anomaly.md).

---

## Ramps up

### Two modules, one job

`crawfish.observer` is the **rules engine** — `Observer`, `Rule` and its subclasses,
`ObserverContext`. `crawfish.observe` is the **surface** — `ObserverSurface`, the
`ObserverEvent`/`RunInfo` data shapes, `Severity`, and `parse_since`. The engine imports
the surface, not the reverse. Keep the split in mind: a rule consumes `RunInfo` and
produces `ObserverEvent`, both defined in `observe`.

### The surface is a facade over the Store (ADR 0008)

`ObserverSurface` owns no storage. It serialises `RunInfo` through `Store.put_record` /
`Store.list_records` (keyed by `run_id`, kind `"run_info"`) and rides `ObserverEvent`s on
the existing event ledger under a synthetic stream id `observer:<pipeline>`. That
namespace cannot collide with a real run's stream — run ids come from `new_id()`, which
never produces a colon-prefixed value. Keeping every write inside the `Store` seam is what
lets a `ScrubbingStore` wrapper redact before persistence with no extra code path. See
[ADR 0008](../architecture/decisions/0008-observer-surface-facade-over-store.md).

When an emitted event names a specific `run_id`, `emit` *also* writes a typed `OBSERVER`
emission onto that run's own stream, so the finding joins the unified emission stream the
inspector reads — the `observer:<pipeline>` copy is still written unchanged.

### How each rule decides

All three rules read from an `ObserverContext` — the window under judgement: the pipeline
name, its recent `runs`, recent `events`, and `now`. `ObserverContext.runs_since(window)`
filters `runs` to those whose `started_at` is at or after the `parse_since` cutoff.

- **`FailureRateAbove(threshold, window="-1h")`** — over runs in `window` that have
  *finished* (`finished_at is not None`), computes `failed / total`. Fires `CRITICAL` only
  when that rate is **strictly greater** than `threshold`. Empty window → no event.
- **`CostSpike(usd, window="-5m")`** — sums `cost_usd` over every run in `window`. Fires
  `WARN` when the sum is **≥** `usd`. (Note the asymmetry with `FailureRateAbove`: cost is
  inclusive, failure rate is exclusive.)
- **`StuckRun(seconds)`** — scans *all* `runs` (not windowed) for any that are still
  `running` with `finished_at is None` and have aged past `seconds`. Reports `CRITICAL` on
  the worst (oldest) one. No `window` parameter.

A rule returning `None` means "nothing to report"; `Observer.evaluate` emits only the
non-`None` events.

### The judge is data-bounded, never instruction-bound

If an `Observer` is given a `judge` (a `Definition`) and a `judge_runtime`, `evaluate`
also runs it: recent runs are summarised to text and passed as **fluid inputs** — model
inputs treated as untrusted data, never as commands. Its spend is capped by a
`CostBudget(limit_usd=judge_cost_cap_usd)` (default `$0.50`), so a runaway judge is
impossible. The judge's free-text finding is run through `redact(...)` and truncated to
300 chars before it becomes an `ObserverEvent` — defence in depth for the case where the
observer reads a raw (unwrapped) store. `judge_flag` decides flagged-vs-clean; by default
any reply that isn't one of `{"", "ok", "pass", "none", "fine", "good"}` is flagged.

### `parse_since` never raises

`parse_since` is called inside dashboard and poll loops, so a malformed window must not
crash a render. Accepted forms: `None` → `0.0` (epoch 0, i.e. *everything*); an absolute
epoch `int`/`float` → itself; a relative string `"-<n><unit>"` where unit ∈
`{s, m, h, d}` → `now - n·unit`; any other parseable string → that float (an absolute
epoch). Anything malformed (`"-xh"`, `"garbage"`) falls back to `0.0` rather than raising.
`now` defaults to wall-clock `time.time()`; pass it explicitly for deterministic results.

### Enums are `(str, Enum)`

`Severity` subclasses `(str, Enum)`, so `Severity.WARN == "warn"` and Pydantic coerces raw
strings into members at the boundary (the project-wide convention; Ruff `UP042` is disabled).

---

## API reference

### `Severity`

`class Severity(str, Enum)` — how loudly an observer event should be surfaced.

| Member | Value | Meaning |
| --- | --- | --- |
| `Severity.INFO` | `"info"` | Informational; nothing wrong. |
| `Severity.WARN` | `"warn"` | Worth attention (e.g. cost spike, quality drop). |
| `Severity.CRITICAL` | `"critical"` | Failure-rate breach or a stuck run. |

### `ObserverEvent`

`class ObserverEvent(BaseModel)` — a structured, append-only finding emitted by an
observer or a node.

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `pipeline` | `str` | — (required) | Pipeline the finding concerns. Stable identifier. |
| `kind` | `str` | — (required) | Dotted kind, e.g. `"cost.spike"`, `"failure.rate"`, `"quality.low"`. Stable. |
| `detail` | `str` | `""` | Free-form, human-readable; scrubbed on write under a `ScrubbingStore`. |
| `severity` | `Severity` | `Severity.INFO` | How loudly to surface. |
| `observer` | `str \| None` | `None` | Which observer produced it (e.g. `"rule:cost_spike"`, `"judge"`). |
| `run_id` | `str \| None` | `None` | The run it concerns, if any; triggers the extra emission write. |
| `ts` | `float` | `time.time()` | Event timestamp (epoch seconds). |
| `data` | `dict[str, JSONValue]` | `{}` | Free-form structured payload; scrubbed on write. |
| `id` | `str` | `new_id()` | Opaque event id. |

### `RunInfo`

`class RunInfo(BaseModel)` — the per-run summary a dashboard and `craw manage` read.

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `pipeline` | `str` | — (required) | Owning pipeline. |
| `run_id` | `str` | — (required) | Run identifier; the record key. |
| `status` | `str` | `"running"` | One of `running` \| `done` \| `failed` \| `needs_retry`. |
| `backend` | `str` | `"command"` | Which runtime backend ran it. |
| `version` | `str` | `""` | Pipeline version. |
| `cost_usd` | `float` | `0.0` | Spend for this run. |
| `items` | `int` | `0` | Items processed. |
| `started_at` | `float` | `time.time()` | Start (epoch seconds); windowing key. |
| `finished_at` | `float \| None` | `None` | End, or `None` while still running. |

### `parse_since`

```python
def parse_since(
    since: str | float | int | None = None,
    *,
    now: float | None = None,
) -> float
```

Resolve a `since` argument to an epoch-seconds threshold. `None` → `0.0` (everything); an
absolute epoch number → itself; a relative `"-<n><unit>"` (`unit ∈ {s,m,h,d}`) →
`(now or time.time()) - n·unit`; any other parseable string → its float. Malformed input
returns `0.0` and never raises.

### `ObserverContext`

`@dataclass class ObserverContext` — the window a rule judges.

| Field | Type | Notes |
| --- | --- | --- |
| `pipeline` | `str` | Pipeline under judgement. |
| `runs` | `list[RunInfo]` | Recent run summaries. |
| `events` | `list[ObserverEvent]` | Recent observer events — a hook for custom rules (debounce/escalate); built-ins ignore it. |
| `now` | `datetime` | The evaluation instant. |

```python
def runs_since(self, window: str) -> list[RunInfo]
```

Runs whose `started_at` is at or after `parse_since(window, now=self.now.timestamp())`.

### `Rule`

`class Rule(ABC)` — a cheap, deterministic check over recent runs. Has a `kind: str`
class attribute and one abstract method:

```python
@abstractmethod
def evaluate(self, octx: ObserverContext) -> ObserverEvent | None: ...
```

Returns an `ObserverEvent` when it trips, else `None`.

### `FailureRateAbove`

```python
class FailureRateAbove(Rule):  # kind = "failure.rate"
    def __init__(self, threshold: float, *, window: str = "-1h") -> None
```

Over *finished* runs in `window`, fires `Severity.CRITICAL` when `failed/total >
threshold` (strict). Empty window → `None`. `data`: `{"rate", "failed", "total"}`.

### `CostSpike`

```python
class CostSpike(Rule):  # kind = "cost.spike"
    def __init__(self, usd: float, *, window: str = "-5m") -> None
```

Sums `cost_usd` over all runs in `window`; fires `Severity.WARN` when the sum `≥ usd`
(inclusive). `data`: `{"spent_usd", "threshold_usd"}`.

### `StuckRun`

```python
class StuckRun(Rule):  # kind = "run.stuck"
    def __init__(self, seconds: float) -> None
```

Scans all runs (not windowed) for `status == "running"`, `finished_at is None`, aged
`> seconds`; fires `Severity.CRITICAL` on the oldest. `data`: `{"run_id", "age_s"}`.

### `ObserverSurface`

`class ObserverSurface` — read/write facade over the run-info surface, scoped to one
tenant.

```python
def __init__(self, store: Store, *, org_id: str = "local") -> None
```

| Method | Returns | Purpose |
| --- | --- | --- |
| `emit(event)` | `None` | Append an `ObserverEvent` to `observer:<pipeline>`; also writes a typed emission to the run stream when `event.run_id` is set. |
| `events(pipeline, *, since=None, kind=None, now=None)` | `list[ObserverEvent]` | Events for `pipeline`, oldest first, filtered by `since`/`kind`. |
| `put_run_info(info)` | `None` | Upsert a `RunInfo` record (idempotent on `run_id`). |
| `get_run_info(run_id)` | `RunInfo \| None` | Fetch one run's record. |
| `run_info(pipeline=None, *, since=None, now=None)` | `list[RunInfo]` | Run-info records, **newest first**, optionally scoped to one pipeline/window. |

### `Observer`

`class Observer` — watch one pipeline: run rules (and an optional LLM judge) on a poll
interval.

```python
def __init__(
    self,
    watch: str,
    *,
    poll: str | CronSchedule | None = None,
    rules: Sequence[Rule] = (),
    judge: Definition | None = None,
    judge_runtime: AgentRuntime | None = None,
    judge_cost_cap_usd: float = 0.50,
    judge_flag: JudgeFlagFn = _default_judge_flag,
    org_id: str = "local",
    lookback: str = "-24h",
) -> None
```

A `str` `poll` is wrapped in `CronSchedule`. Key methods:

| Method | Returns | Purpose |
| --- | --- | --- |
| `poll_due(now)` | `bool` | Whether the schedule fires at `now` (always `True` if no schedule). |
| `evaluate(store, *, now=None, run_judge=True)` | `list[ObserverEvent]` | Run every rule (and the judge, if configured) once; emit + return findings. `now` defaults to `datetime.now(UTC)`. |
| `watch_loop(store, *, max_polls=None, now_fn=None, sleep_fn=None, stop_flag=None)` | `int` | Block, evaluating on each poll tick; returns the number of evaluations. Injectable clock/sleep/stop for testing. |

---

## Example

`parse_since` on a few inputs, then two rules over synthetic runs — all pure, fixed clock,
no store and no model.

```python
from datetime import datetime, UTC

from crawfish.observe import parse_since, RunInfo
from crawfish.observer import ObserverContext, FailureRateAbove, CostSpike

NOW = 1_000_000.0  # fixed epoch — no real clock

# parse_since: relative windows resolve against `now`; None/garbage -> everything (0.0).
print(int(NOW - parse_since("-1h", now=NOW)))   # window width, seconds
print(int(NOW - parse_since("-30m", now=NOW)))
print(parse_since(None))
print(parse_since("garbage", now=NOW))

# Four finished runs: 2 failed, $3.10 spent total.
runs = [
    RunInfo(pipeline="triage-bot", run_id="r1", status="done",   cost_usd=0.40, started_at=NOW-120, finished_at=NOW-60),
    RunInfo(pipeline="triage-bot", run_id="r2", status="failed", cost_usd=0.50, started_at=NOW-110, finished_at=NOW-50),
    RunInfo(pipeline="triage-bot", run_id="r3", status="failed", cost_usd=1.20, started_at=NOW-100, finished_at=NOW-40),
    RunInfo(pipeline="triage-bot", run_id="r4", status="done",   cost_usd=1.00, started_at=NOW-90,  finished_at=NOW-30),
]
octx = ObserverContext(
    pipeline="triage-bot", runs=runs, events=[], now=datetime.fromtimestamp(NOW, UTC)
)

for rule in (FailureRateAbove(0.2, window="-1h"), CostSpike(2.0, window="-5m")):
    ev = rule.evaluate(octx)
    print(f"{ev.kind} [{ev.severity.value}] {ev.detail}")
```

??? success "▶ Output"

    ```text
    3600
    1800
    0.0
    0.0
    failure.rate [critical] 2/4 runs failed (50% > 20%)
    cost.spike [warn] $3.10 spent in 5m (≥ $2.00)
    ```
