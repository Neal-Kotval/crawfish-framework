# Batch & execution

The fan-out engine: how one agent is run over many items, how multi-step work is
ordered, and how per-item progress is retried and persisted so a crashed run can
resume. These live in `crawfish.batch`, `crawfish.executor`, `crawfish.ledger`,
`crawfish.retry`, and `crawfish.workflow`.

**Symbols on this page:** `Batch` · `Task` · `Anomaly` · `DependencyGraph` ·
`CycleError` · `Roadmap` · `ExecutionPlan` · `BatchExecutor` · `BatchRunResult` ·
`ExecutionLedger` · `ExecState` · `RetryPolicy` · `ItemResult` · `ItemStatus` ·
`Workflow`

---

## Core

A **batch** runs one agent definition over a set of items. A source pulls in a list —
say 500 pull requests — and the batch **fans out**: it makes one independent run per
item, each run seeded with that item's own data. `Batch` is the hand-wired assembly
point. You attach sources to it, and each item becomes its own run.

Inside a batch, each item's run is tracked as a **task** (`Task`) — an id, a
description, and an optional list of other task ids it is **blocked by** (must wait
for). An **anomaly** (`Anomaly`) flags an item that went wrong — currently a run that
failed — so a batch of 500 can surface the 3 that broke without you scanning all 500.

When work has order — fetch *before* parse, parse *before* write — that order is a
**dependency graph** (`DependencyGraph`): nodes with edges saying "this blocks that".
Turning the graph into runnable order produces **layers** where every task in a layer
can run in parallel, and each layer waits for the one
before. That ordered result is an **execution plan** (`ExecutionPlan`). If the
dependencies loop back on themselves (A waits for B, B waits for A) there is no valid
order, so the sort raises a **`CycleError`**. (`Roadmap` is a small placeholder for
milestone metadata an executor may carry; it does no scheduling yet.)

A **batch executor** (`BatchExecutor`) takes a plan and actually runs the batch through
a fixed pool of workers draining a queue, so a 10,000-item fan-out is rate-limited
rather than launched all at once. It returns a **`BatchRunResult`**: the successful
outputs, a per-item result for every item, and the **dead letters** — items that failed
permanently and were set aside instead of halting the whole batch.

Each item's outcome is an **`ItemResult`** with an **`ItemStatus`** of either `OK` or
`DEAD` (failed past all retries). Retrying is governed by a **`RetryPolicy`**: how many
attempts, and how long to wait between them using **exponential backoff** (each wait is
longer than the last, up to a ceiling).

Progress is written down as it happens in the **execution ledger** (`ExecutionLedger`),
an append-only log in the `Store` (Crawfish's persistence layer). It records each
pipeline, run, and fan-out item with an **`ExecState`** — `running`, `done`, `failed`,
or `needs_retry`. Because progress is durable, a run interrupted by a crash can
**resume**: skip the items already marked `done` and redo only the rest. Re-running the
same work produces the same result without duplicate side effects — that property is
called **idempotency**, and it is what makes resume and replay safe.

A **workflow** (`Workflow`) is the whole pipeline as one deployable unit: an ordered
list of steps (source, filter, batch, aggregator, sink) with data threaded stage to
stage. It checkpoints after each stage, so a crash mid-workflow resumes from the last
completed stage.

---

## Ramps up

### Fan-out happens in `_gather_inputs`, one run per item

`Batch` separates inputs into **static** values (shared by every item — a target board,
a repo link) and **fluid** value sets (one dict per item). A source marked `multi`
expands via `fan_out` into N per-item value sets; a single-value source contributes to
the shared base. Each run's inputs are `{**base_values, **item_values}` — statics merged
with that item's fluids. With no multi source there is exactly one item set (`[{}]`), so
one run; with a multi source of N items there are N runs.

Wiring is **type-checked at assembly**, not at run time: `check_wiring()` resolves every
definition input against what the wired sources provide and raises `WireError` on a
missing required input or an incompatible type — structurally, via
[`parameters_compatible`](core-types.md#parameters_compatible), never string equality.
`Batch.run` calls `check_wiring()` again at the top as defence in depth.

The batch's `cost_budget` (a token/$ ceiling) is carried onto every child run's
`RunContext` and **shared across all runs** — the ceiling is for the whole fan-out, not
per item. If no batch budget is set, the parent context's budget is used.

### Scheduling: Kahn's algorithm, sorted for determinism

`DependencyGraph.topo_layers()` is [Kahn's algorithm](https://en.wikipedia.org/wiki/Topological_sorting#Kahn's_algorithm):
start from nodes with no blockers (in-degree 0), peel them off as one layer, decrement
their dependents, repeat. Each layer's nodes are **sorted** before being appended, so
the plan is deterministic for a given graph. If, after the sweep, fewer nodes were seen
than exist, some nodes never reached in-degree 0 — they form a cycle — and `CycleError`
(a `ValueError` subclass) is raised.

`BatchExecutor.schedule(tasks)` builds the graph from each `Task.blocked_by`. An edge is
added only when the blocker id is *also* in the task set — a `blocked_by` pointing at an
unknown id is ignored, never an error. The result is an `ExecutionPlan` of id layers.

> Note: `schedule()` only orders task ids; the executor's `run()` does not consume the
> plan's layering to gate dispatch — it drains every item through the worker pool
> concurrently up to `max_concurrency`. The plan is the schedule view; the work queue is
> the dispatch backbone.

### Backpressure, hard-kill, and dead-lettering

`BatchExecutor.run` loads all per-item value sets into an `asyncio.Queue`, then starts
`max_concurrency` workers (default 8) that pull from it. A fixed pool draining a queue is
**backpressure**: at most `max_concurrency` runs are ever in flight, so a huge fan-out is
rate-limited instead of exploding.

Two failure modes are handled differently:

- **A `BudgetExceeded`** (the cost ceiling was hit) is a runaway — it is *not* retried,
  it propagates, the worker drains the remaining queue, the pipeline is marked `FAILED`,
  and the exception is re-raised. Hard-kill.
- **Any other item failure** retries per the `RetryPolicy`; on exhaustion the item is
  **dead-lettered** (recorded in the store, keyed `{batch_id}:{item_id}`) and the batch
  *continues*. A dead item becomes an `ItemResult(status=DEAD)`, never a halt.

`replay()` re-runs only the dead-lettered items: it reads the dead-letter list, clears
those records, and calls `run(only_items=...)` scoped to them. Sink idempotency makes
re-running safe. `only_items` is the same mechanism a resume uses to skip completed work.

### The ledger pins a version and survives restarts

`ExecutionLedger` writes four record kinds to the `Store`: `ledger_pipeline` (status +
**pinned version** + total items + completed step indices), `ledger_item` (per-fan-out
status), and `ledger_run` (per-run status tagged with its **backend** name). When a
pipeline starts it pins the definition version; an in-flight pipeline stays on the
version it began with — a redeploy applies only to *new* pipelines.

`reconcile()` is restart recovery. It scans runs still marked `RUNNING` and splits them:
runs on an **ephemeral backend** (`command` — the `claude -p` subprocess — or `mock`,
both of which die with the engine) are flipped to `NEEDS_RETRY` because their session is
gone and must never be silently lost; runs on a resumable backend are left for resume.
It returns `{"retried": [...], "resumable": [...]}`.

### Retry math

`RetryPolicy.delay_for(attempt)` is `min(base_delay * factor**attempt, max_delay)` —
classic exponential backoff with a ceiling. With the default `base_delay=0.0` every delay
is `0.0` (no wait), which is why tests run fast; `BatchExecutor` defaults its policy to
`base_delay=0.5` for production-grade backoff. `run_with_retry` loops up to
`max_attempts`, sleeping `delay_for(attempt)` between tries, but **never retries**
`BudgetExceeded` or `Cancelled` — those re-raise immediately. The `sleep` callable is
injectable, so tests pass a no-op and stay deterministic.

### Workflow checkpoints per stage

`Workflow.run` walks `steps` in order, threading the list of `Output`s from one stage
into the next. After each stage it `checkpoint_step`s the index and saves the current
outputs (`workflow_state` record). With `resume=True` it loads completed step indices and
the saved outputs and skips finished stages. Adjacency is type-checked at assembly by
`check_types()` (producer's output schema ↔ consumer's inputs), raising `WireError` on a
mismatch. Step dispatch by kind: `Source` fetches (and fans out if `multi`), `Filter`
keeps items whose value passes `predicate` (preserving lineage + taint), `Batch` runs the
definition per item carrying the source item's lineage forward, `Aggregator` reduces all
items to one, `Sink` writes each item and passes them through unchanged.

---

## API reference

### `Task`

`class Task(BaseModel)` — one unit of work in a batch / scheduling graph.

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `id` | `str` | `new_id()` | Opaque task id (UUID4). |
| `description` | `str` | `""` | Human-readable label. |
| `blocked_by` | `list[str]` | `[]` | Ids of tasks that must finish first. |

### `Anomaly`

`class Anomaly(BaseModel)` — an item flagged as odd. All fields required.

| Field | Type | Notes |
| --- | --- | --- |
| `task_id` | `str` | The task this anomaly concerns. |
| `kind` | `str` | Anomaly class, e.g. `"run_failed"`. |
| `detail` | `str` | Human-readable explanation. |

### `Batch`

`class Batch(Node)` — fans one `Definition` over many items, wired from sources/outputs.

```python
Batch(
    definition: Definition,
    name: str = "batch",
    *,
    runtime: AgentRuntime | None = None,
    cost_budget: CostBudget | None = None,
) -> None
```

Key members:

- `add_input(item: Source | Output) -> Batch` — wire a source/upstream output; returns
  `self` for chaining.
- `check_wiring() -> None` — type-check all wires at assembly; raises `WireError`.
- `async run(ctx, runtime=None) -> list[Output]` — fan out and execute; one run per item,
  shared `cost_budget`. Raises `ValueError` if no runtime.
- `detect_anomalies() -> list[Anomaly]` — one `Anomaly(kind="run_failed")` per failed run.

`kind` is `NodeKind.BATCH`. Populates `tasks`, `runs`, `outputs` as it runs.

### `CycleError`

`class CycleError(ValueError)` — raised when a dependency graph contains a cycle.

### `DependencyGraph`

`class DependencyGraph` — `add_node(node: str)`, `add_edge(blocker: str, blocked: str)`,
`topo_layers() -> list[list[str]]`. Edges are `(blocker, blocked)`. `topo_layers` returns
parallelizable layers (each sorted) via Kahn's algorithm; raises `CycleError` on a cycle.

### `Roadmap`

`class Roadmap(BaseModel)` — `milestones: list[dict[str, JSONValue]] = []`. A placeholder
carried by `BatchExecutor`; no scheduling behaviour yet.

### `ExecutionPlan`

`class ExecutionPlan(BaseModel)` — `layers: list[list[str]] = []`. Ordered layers of task
ids; each layer is internally parallelizable.

### `BatchExecutor`

`class BatchExecutor` — schedules and runs a `Batch`.

```python
BatchExecutor(
    definition: Definition,
    *,
    max_concurrency: int = 8,
    retry_policy: RetryPolicy | None = None,   # defaults to RetryPolicy(base_delay=0.5)
    runtime: AgentRuntime | None = None,
) -> None
```

| Method | Signature | Behaviour |
| --- | --- | --- |
| `schedule` | `(tasks: list[Task]) -> ExecutionPlan` | Build graph from `blocked_by`; topo-sort. |
| `run` | `async (batch, ctx, runtime=None, *, only_items: set[str] \| None = None) -> BatchRunResult` | Drain item queue across workers; retry, dead-letter, ledger. Re-raises `BudgetExceeded`. |
| `replay` | `async (batch, ctx, runtime=None) -> BatchRunResult` | Re-run only dead-lettered items. |

### `BatchRunResult`

`@dataclass BatchRunResult` — the outcome of a batch run.

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `outputs` | `list[Output]` | `[]` | Outputs of `OK` items only. |
| `items` | `list[ItemResult]` | `[]` | One per processed item, sorted by id. |
| `dead_letters` | `list[dict[str, JSONValue]]` | `[]` | Permanently-failed items. |

### `RetryPolicy`

`@dataclass RetryPolicy` — exponential backoff config.

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `max_attempts` | `int` | `3` | Total tries before giving up. |
| `base_delay` | `float` | `0.0` | Base seconds; `0.0` means no wait. |
| `factor` | `float` | `2.0` | Backoff multiplier per attempt. |
| `max_delay` | `float` | `30.0` | Delay ceiling in seconds. |

`delay_for(attempt) -> float` = `min(base_delay * factor**attempt, max_delay)`.

### `ItemStatus`

`class ItemStatus(str, Enum)`:

| Member | Value | Meaning |
| --- | --- | --- |
| `ItemStatus.OK` | `"ok"` | Item succeeded. |
| `ItemStatus.DEAD` | `"dead"` | Exhausted retries → dead-lettered. |

### `ItemResult`

`@dataclass ItemResult` — partial-success unit surfaced in batch results.

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `item_id` | `str` | — (required) | Item id (string index in the fan-out). |
| `status` | `ItemStatus` | — (required) | `OK` or `DEAD`. |
| `value` | `JSONValue` | `None` | Output value when `OK`. |
| `error` | `str \| None` | `None` | Error string when `DEAD`. |
| `attempts` | `int` | `0` | Attempt count. |

Module helpers: `run_with_retry(factory, policy, *, sleep=asyncio.sleep) -> R` (retries,
never on `BudgetExceeded`/`Cancelled`), `dead_letter(ctx, *, batch_id, item_id, error,
payload=None, attempts=0)`, `list_dead_letters(ctx, batch_id) -> list[dict]`.

### `ExecState`

`class ExecState(str, Enum)` — execution state in the ledger:

| Member | Value | Meaning |
| --- | --- | --- |
| `ExecState.RUNNING` | `"running"` | In progress. |
| `ExecState.DONE` | `"done"` | Completed successfully. |
| `ExecState.FAILED` | `"failed"` | Failed terminally. |
| `ExecState.NEEDS_RETRY` | `"needs_retry"` | Orphaned by a crash; must be retried. |

### `ExecutionLedger`

`class ExecutionLedger` — `ExecutionLedger(store: Store, *, org_id: str = "local")`.
Store-backed execution state.

| Method | Signature | Notes |
| --- | --- | --- |
| `start_pipeline` | `(pipeline_id, version, *, total_items=0) -> None` | Pin version, status `RUNNING`. |
| `pinned_version` | `(pipeline_id) -> str \| None` | The version the pipeline started on. |
| `checkpoint_step` | `(pipeline_id, step_index) -> None` | Mark a workflow stage done. |
| `completed_steps` | `(pipeline_id) -> set[int]` | Completed stage indices. |
| `finish_pipeline` | `(pipeline_id, status=ExecState.DONE) -> None` | Set terminal status. |
| `mark_item` | `(pipeline_id, item_id, status) -> None` | Per-fan-out-item cursor. |
| `completed_items` | `(pipeline_id) -> set[str]` | Item ids marked `DONE`. |
| `record_run` | `(run_id, *, backend, status, version) -> None` | Per-run state + backend. |
| `reconcile` | `() -> dict[str, list[str]]` | After restart: ephemeral `RUNNING`→`NEEDS_RETRY`; returns `{"retried", "resumable"}`. |

### `Workflow`

`class Workflow` — a versioned pipeline of steps, deployable as a unit.

```python
Workflow(
    prompt: str = "",
    steps: list[Node] | None = None,
    *,
    name: str = "workflow",
    runtime: AgentRuntime | None = None,
    version: str = "0.1",
) -> None
```

- `check_types() -> None` — type-check adjacent steps; raises `WireError`.
- `async run(prompt=None, *, ctx=None, runtime=None, resume=False) -> list[Output]` —
  run steps in order, checkpointing each stage. With `resume=True`, skip completed stages
  and reload saved outputs. A default `SqliteStore`-backed `RunContext` is created if none
  is passed.

---

## Example

Build a dependency graph, derive a topologically-sorted `ExecutionPlan`, reject a cycle,
and run a `RetryPolicy` over a flaky pure function to watch `ItemStatus` transitions — all
deterministic, no runtime needed.

```python
import asyncio
from crawfish.executor import DependencyGraph, ExecutionPlan, BatchExecutor, CycleError
from crawfish.batch import Task
from crawfish.retry import RetryPolicy, run_with_retry, ItemResult, ItemStatus
from crawfish.ledger import ExecState

# A dependency graph -> parallelizable layers -> an ExecutionPlan.
g = DependencyGraph()
g.add_edge("fetch", "parse")      # fetch blocks parse
g.add_edge("fetch", "validate")   # fetch blocks validate
g.add_edge("parse", "write")      # parse blocks write
g.add_edge("validate", "write")   # validate blocks write
plan = ExecutionPlan(layers=g.topo_layers())
for i, layer in enumerate(plan.layers):
    print(f"layer {i}: {layer}")

# BatchExecutor.schedule turns Tasks (with blocked_by) into the same plan shape.
a = Task(id="a", description="first")
b = Task(id="b", description="second", blocked_by=["a"])
c = Task(id="c", description="third", blocked_by=["a"])
ex = BatchExecutor(definition=None)  # schedule() never touches the definition
print("schedule:", ex.schedule([a, b, c]).layers)

# A cycle has no valid order -> CycleError.
cyc = DependencyGraph()
cyc.add_edge("x", "y")
cyc.add_edge("y", "x")
try:
    cyc.topo_layers()
except CycleError as e:
    print("CycleError:", e)

# RetryPolicy over a flaky pure function: fails twice, then succeeds -> OK.
attempts = {"n": 0}
async def flaky():
    attempts["n"] += 1
    if attempts["n"] < 3:
        raise RuntimeError(f"transient {attempts['n']}")
    return "ok-value"

async def noop_sleep(_):  # deterministic: no real waiting
    return None

policy = RetryPolicy(max_attempts=3, base_delay=0.0)
print("delays:", [policy.delay_for(i) for i in range(3)])
val = asyncio.run(run_with_retry(flaky, policy, sleep=noop_sleep))
ok = ItemResult(item_id="0", status=ItemStatus.OK, value=val)
print("item:", ok.item_id, ok.status.value, ok.value, "after", attempts["n"], "tries")

# Exhausting retries surfaces a DEAD item instead of crashing the batch.
attempts2 = {"n": 0}
async def always_fail():
    attempts2["n"] += 1
    raise RuntimeError("boom")
try:
    asyncio.run(run_with_retry(always_fail, RetryPolicy(max_attempts=2, base_delay=0.0), sleep=noop_sleep))
except RuntimeError as e:
    dead = ItemResult(item_id="1", status=ItemStatus.DEAD, error=str(e))
    print("item:", dead.item_id, dead.status.value, dead.error, "after", attempts2["n"], "tries")

print("ExecState:", [s.value for s in ExecState])
```

??? success "▶ Output"

    ```text
    layer 0: ['fetch']
    layer 1: ['parse', 'validate']
    layer 2: ['write']
    schedule: [['a'], ['b', 'c']]
    CycleError: dependency graph has a cycle
    delays: [0.0, 0.0, 0.0]
    item: 0 ok ok-value after 3 tries
    item: 1 dead boom after 2 tries
    ExecState: ['running', 'done', 'failed', 'needs_retry']
    ```
