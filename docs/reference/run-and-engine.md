# Run & engine

One **run** is a single execution of a pipeline against one set of inputs: it binds
those inputs, drives the work, tracks where it is in its lifecycle, and records what
happened. The **engine** is the thin bootstrap that threads a list of steps together —
the machinery behind `craw run`.

**Symbols on this page:** `Run` · `RunStatus` · `InputBindingError` · `RunSuspended` ·
`Engine` · `run_pipeline`

---

## Core

A **definition** describes a pipeline: its input slots, its output slots, and the agent
team that does the work. A `Run` is *one execution* of a definition — you hand it a
definition plus a dictionary of input values, and it carries that single task from start
to finish.

Every run moves through a fixed set of **statuses**, named by `RunStatus`: it starts
`PENDING`, becomes `RUNNING` while the team works, and ends `DONE` (succeeded), `FAILED`
(errored or over budget), or `SUSPENDED`. **Suspended** means the run hit an approval
gate and is idling: its state is saved to durable storage and *no compute is spent* while
it waits for a human to approve. The run can be rebuilt later from that saved state.

Before a run does any work it **validates** its inputs, in two steps:

- **Presence** — every required slot must actually be filled. A missing one raises
  `InputBindingError`.
- **Type** — each filled value must match the type its slot declares. A wrong type raises
  `InputValidationError` (a sibling exception, not on this page).

This validation happens *before any model is called*, so a malformed request fails fast
and cheaply.

When a run is configured to require approval and you run it without approving,
`Run.execute` raises `RunSuspended` — the signal that the run parked itself on the gate
rather than spending money.

The **engine** sits underneath all of this. A pipeline, at its most basic, is an ordered
list of **steps**, where each step is an async function `(ctx, inputs) -> outputs` that
takes the previous step's outputs and returns the next. `Engine.run_pipeline` walks that
list, threading outputs forward, and the module-level `run_pipeline` is a one-line
convenience that builds a default engine for you. An empty list of steps is a valid
pipeline: it runs end to end and returns nothing. This is the honest minimum behind
`craw run`; the richer typed `Run`/definition machinery builds on the same contract.

---

## Ramps up

### A run is durable, not just in-memory

`Run` is built to survive a process restart. As it changes status it writes a record to
the `Store` (`_persist`), and `Run.restore(store, run_id, definition)` rebuilds a run
from that record — recovering its status so a suspended or interrupted run can be picked
back up. That is why `SUSPENDED` is a real status and not an exception side-effect: the
state genuinely lives in storage between the suspend and the eventual approval.

### Fluid inputs are session data, and they taint the output

A run binds **fluid** inputs (untrusted, per-item session data — see
[core types](core-types.md)) as *data the model reads*, never concatenated into the
instruction prompt. That boundary is enforced by the prompt compiler, not by `Run`
itself. When the run produces its typed `Output`, it marks the output **tainted** if any
input was fluid *or* the team consumed any tool/MCP result (a malicious tool response is
itself an injection vector). The taint marker then propagates onto the event ledger.

### Validation failures route, output failures repair

Two different validation moments, two different exceptions:

- **Input** validation runs in `Run.validate()` before execution: `InputBindingError`
  for an unbound required slot, `InputValidationError` for a wrong-typed value.
- **Output** validation runs after the model replies. If the output fails its declared
  schema, the run's `on_invalid` policy (a `ValidationAction`) decides: `RETRY` re-runs
  the team, `REPAIR` re-prompts the model once with the error fed back as ordinary fluid
  data, `DEAD_LETTER` gives up. The repair call is metered and bounded by
  `ctx.cost_budget`, so the overshoot is at most one extra call.

A run is also **hard-killed at the cost cap**: if execution raises `BudgetExceeded` the
run goes `FAILED` with telemetry captured, rather than overspending.

### The engine emits an event per boundary

`Engine.run_pipeline` appends events to the store at every boundary — `pipeline.start`,
then `step.start`/`step.done` per step, then `pipeline.done` — and checks the
`RunContext` cancel token before each step, so a cancelled run stops between steps. It
runs under a single `RunContext`: pass your own (with a cost budget and cancel token) or
let it build a default one over the engine's store.

---

## API reference

### `RunStatus`

`class RunStatus(str, Enum)` — the lifecycle of a run.

| Member | Value | Meaning |
| --- | --- | --- |
| `RunStatus.PENDING` | `"pending"` | Created, not yet started. The status a fresh `Run` carries. |
| `RunStatus.RUNNING` | `"running"` | The team is executing. |
| `RunStatus.DONE` | `"done"` | Completed successfully; `Run.output` is set. |
| `RunStatus.FAILED` | `"failed"` | Errored — including over-budget (`BudgetExceeded`) and exhausted output validation. |
| `RunStatus.SUSPENDED` | `"suspended"` | Idling on an approval gate; state held durably, no compute spent. |

### `Run`

`class Run` — one durable execution of a `Definition` against one input set: *"an agent
team performing a single task."* Constructor:

```python
Run(
    definition: Definition,
    inputs: dict[str, JSONValue] | None = None,
    *,
    runtime: AgentRuntime | None = None,
    requires_approval: bool = False,
    on_invalid: ValidationAction = ValidationAction.DEAD_LETTER,
    retry_policy: RetryPolicy | None = None,
    registry: TypeRegistry | None = None,
    validate_input_types: bool = True,
    validate_output_schema: bool = True,
    id: str | None = None,
)
```

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `id` | `str` | `new_id()` | Opaque run id; reused by `restore`. |
| `definition` | `Definition` | — (required) | The pipeline this run executes. |
| `inputs` | `dict[str, JSONValue]` | `{}` | Bound input values, copied into the run. |
| `runtime` | `AgentRuntime \| None` | `None` | The runtime seam; may instead be passed to `execute`. |
| `requires_approval` | `bool` | `False` | If set, `execute` suspends unless `approve=True`. |
| `on_invalid` | `ValidationAction` | `DEAD_LETTER` | How an output-schema failure is handled (`RETRY`/`REPAIR`/`DEAD_LETTER`). |
| `retry_policy` | `RetryPolicy` | `RetryPolicy()` | Used when `on_invalid` is `RETRY`. |
| `registry` | `TypeRegistry` | `default_registry` | Resolves input/output types. |
| `validate_input_types` | `bool` | `True` | Opt out of input *type* checks (presence still enforced). |
| `validate_output_schema` | `bool` | `True` | Opt out of output schema validation (e.g. a free-text classifier). |
| `status` | `RunStatus` | `RunStatus.PENDING` | Current lifecycle status. |
| `output` | `Output[JSONValue] \| None` | `None` | The typed output once `DONE`. |

Key methods:

```python
def validate(self) -> None
```

Fail fast before any model call: presence first (`InputBindingError` for an unbound
required slot), then type (`InputValidationError` for a wrong-typed value, when
`validate_input_types`). Does not change `status`.

```python
async def execute(
    self,
    ctx: RunContext,
    runtime: AgentRuntime | None = None,
    *,
    approve: bool | None = None,
) -> Output[JSONValue]
```

Validate, then run the definition's team on the bound inputs → a typed `Output`. With
`requires_approval` set, `approve` of `None`/`False` suspends the run durably and raises
`RunSuspended`; `True` proceeds. Requires a runtime (here or on the constructor) or
raises `ValueError`. Sets `status` through `RUNNING` → `DONE`/`FAILED`.

```python
@classmethod
def restore(
    cls,
    store: Store,
    run_id: str,
    definition: Definition,
    *,
    runtime: AgentRuntime | None = None,
    org_id: str = "local",
) -> Run
```

Rebuild a run from its persisted record, recovering its `status` (restart recovery).
Raises `KeyError` if no record exists for `run_id`.

### `InputBindingError`

`class InputBindingError(ValueError)` — raised by `Run.validate()` when a required input
slot is unbound before execution. The message lists the missing slot names.

### `RunSuspended`

`class RunSuspended(RuntimeError)` — raised by `Run.execute()` when a run idles on an
approval gate. State is persisted and the status is set to `SUSPENDED`; no compute is
spent.

### `Engine`

`class Engine` — runs a pipeline of steps under a single `RunContext`.

```python
Engine(store: Store | None = None)            # defaults to a fresh SqliteStore

async def run_pipeline(
    self,
    steps: Sequence[Step],
    *,
    ctx: RunContext | None = None,
    seed: list[object] | None = None,
) -> list[object]
```

Walks `steps` in order, threading each step's outputs into the next, starting from
`seed` (default empty). Builds a default `RunContext` over the engine's store if `ctx` is
omitted. Checks the cancel token before each step and appends `pipeline.start` /
`step.start` / `step.done` / `pipeline.done` events to the store. Returns the final
output list. A `Step` is `Callable[[RunContext, list[object]], Awaitable[list[object]]]`.

### `run_pipeline`

```python
async def run_pipeline(steps: Sequence[Step], **kwargs: object) -> list[object]
```

Module-level convenience: builds a default `Engine()` and calls its `run_pipeline`,
forwarding `ctx` / `seed`. An empty `steps` list is a valid no-op that returns `[]`.

---

## Example

The no-op bootstrap pipeline, a pure one-step pipeline, and a `Run`'s lifecycle and
fail-fast input validation — all deterministic, no runtime or model.

```python
import asyncio
from crawfish.engine import run_pipeline
from crawfish.run import Run, RunStatus, InputBindingError
from crawfish.definition.types import Definition
from crawfish.core.types import Parameter

# An empty pipeline is a valid no-op: it runs end to end and returns nothing.
print("empty pipeline:", asyncio.run(run_pipeline([])))

# A pure step threads the seed forward — no model involved.
async def double(ctx, inputs):
    return [x * 2 for x in inputs]

print("one step:", asyncio.run(run_pipeline([double], seed=[1, 2, 3])))

# A Run starts PENDING; its status is a lifecycle enum.
echo = Definition(
    id="echo",
    name="echo",
    inputs=[Parameter(name="text", type="str")],
    outputs=[Parameter(name="text", type="str")],
)
run = Run(echo, inputs={"text": "hello"})
print("initial status:", run.status.value)

# validate() fails fast on an unbound required slot — before any model call.
try:
    Run(echo, inputs={}).validate()
except InputBindingError as e:
    print("binding error:", e)

# A well-bound run validates clean and stays PENDING (validate() doesn't run it).
run.validate()
print("validated; status:", run.status.value)

print("statuses:", [s.value for s in RunStatus])
```

??? success "▶ Output"

    ```text
    empty pipeline: []
    one step: [2, 4, 6]
    initial status: pending
    binding error: missing required input(s): ['text']
    validated; status: pending
    statuses: ['pending', 'running', 'done', 'failed', 'suspended']
    ```
