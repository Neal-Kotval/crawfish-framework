# Nodes — aggregator

The **reduce** half of a pipeline — folding many results into one: after a batch
step splits one job into many, running an agent once per item, an aggregator gathers
those many results back into a single one. These live in `crawfish.nodes.aggregator`.

**Symbols on this page:** `Aggregator` · `collect` · `concat` · `count` · `dedupe` ·
`definition_reducer` · `fan_in`

---

## Core

A pipeline often **fans out** — spreading one job across many parallel runs: a batch
step takes one list of items (a hundred pull requests, a thousand tickets) and runs an
agent on each item independently, producing one result per item. An **aggregator** is
the opposite move — the **fan-in** — it takes that whole group of N results and
collapses it into exactly one. Source →
filter, [batch (fan-out)](nodes-source-filter.md) → **aggregator (fan-in)** → router →
[sink](nodes-router-sink.md) is the spine of every Crawfish pipeline.

Each per-item result arrives wrapped in an **Output**: a frozen (immutable) envelope
carrying the result `value`, the schema describing that value's shape, and the id of
the node that produced it. The aggregator reads the N input Outputs and emits **one**
fresh Output holding the reduced value.

*How* it reduces is pluggable — you hand the aggregator a **reducer**. Crawfish ships
four pure, deterministic built-in reducers:

- **`collect`** — gather the N item values into one list (the identity fan-in: keep
  everything, in order).
- **`concat`** — glue the N item values into one string, end to end, no separator.
- **`count`** — return how many items there were, as an integer.
- **`dedupe`** — like `collect`, but drop repeats, keeping the first occurrence of each.

For richer reductions there is **`definition_reducer`** — a reducer that runs an agent
team to fold the N values into one (e.g. "summarise these hundred findings"). The item
values are fed to the model as **fluid** data — *untrusted session data the model reads
but never obeys* — and the agent's text answer is the reduced result.

Finally, **`fan_in`** is the **barrier** that produces the group of Outputs the
aggregator consumes: it waits for the N concurrent per-item runs to finish and returns
the ones that succeeded.

---

## Ramps up

### The aggregator never mutates its inputs

An `Output` is **frozen** — once produced it cannot be changed, so the upstream values
stay intact for audit. `Aggregator.reduce` therefore never edits an input Output; it
builds a brand-new Output stamped with its own node id in `produced_by`. The reduced
value lands in the new Output's `value`; its declared shape comes from the aggregator's
`output_schema` (empty by default, meaning the shape is left undeclared).

### Sync and async reducers plug in interchangeably

A reducer is anything matching the `Reducer` protocol: a callable taking
`(outputs, ctx)` and returning a value *or* an awaitable of a value. The built-ins are
**pure and synchronous** — they read only `Output.value` and ignore `ctx`. The
Definition-backed reducer is **asynchronous**, because running an agent team is async.
`Aggregator.reduce` handles both: it calls the reducer, and if the result is awaitable
it awaits it. So you swap a built-in for `definition_reducer` without changing the
aggregator.

### Order is preserved everywhere

Every built-in reducer walks the inputs in the order received and preserves it:
`collect` and `dedupe` return values in first-seen order, `concat` joins left to right.
`dedupe` removes a later value only if an **equal** value (`==`) was already seen —
equality on the raw `Output.value`, not on any extracted key.

### `fan_in` is partial-success aware and deterministic

When N items run concurrently, some may fail. `fan_in` runs them with
`asyncio.gather(..., return_exceptions=True)` and then **drops** any result that raised
an exception or resolved to `None`, so one bad item never sinks the whole batch. The
survivors keep their **submission order** (`asyncio.gather` preserves order), which is
what makes the downstream reduction deterministic.

If you pass `quorum=k`, `fan_in` raises `ValueError` when fewer than `k` items
survive — use it to demand "at least k must succeed or fail the run". With no quorum it
returns whatever succeeded, even an empty list.

### Fluid item values stay untrusted in `definition_reducer`

`definition_reducer` packs the N item values into a single input mapping
`{"items": [...]}` and runs the agent team on it. Those values enter as **fluid**
(untrusted) session data — the model treats them as content to read, never as
instructions to follow. This is the same prompt-injection boundary the
[security spine](../architecture/SECURITY.md) enforces throughout; see the
[core types](core-types.md) page for `Flow.FLUID`. The reduced value is the team's
text result.

---

## API reference

### `Reducer`

`class Reducer(Protocol)` — a `@runtime_checkable` protocol for any reduction.

```python
def __call__(
    self, outputs: list[Output[JSONValue]], ctx: RunContext
) -> JSONValue | Awaitable[JSONValue]
```

Reduce `outputs` (in order) to one value. Built-ins are pure and synchronous; the
Definition-backed reducer is asynchronous. `Aggregator.reduce` awaits the result when
it is awaitable, so both shapes plug in interchangeably.

### `collect`

```python
def collect(outputs: list[Output[JSONValue]], ctx: RunContext) -> list[JSONValue]
```

Gather the item values into a list — `[out.value for out in outputs]`. The identity
fan-in: keeps everything, order preserved. `ctx` is unused.

### `concat`

```python
def concat(outputs: list[Output[JSONValue]], ctx: RunContext) -> str
```

Concatenate the item values into one string, each value coerced with `str(...)`, joined
with **no separator** (`"".join(...)`). Order preserved. `ctx` is unused.

### `count`

```python
def count(outputs: list[Output[JSONValue]], ctx: RunContext) -> int
```

Return the number of items — `len(outputs)`. `ctx` is unused.

### `dedupe`

```python
def dedupe(outputs: list[Output[JSONValue]], ctx: RunContext) -> list[JSONValue]
```

List the item values with duplicates removed, **first-seen order preserved**. A value
is a duplicate when it is `==` to one already kept (equality on `Output.value`, no key
function). `ctx` is unused.

### `definition_reducer`

```python
def definition_reducer(definition: Definition, runtime: AgentRuntime) -> Reducer
```

Build a reducer that runs an agent team (`definition`, on `runtime`) to reduce N item
values into one. The returned reducer is **async**: it feeds the values in as the fluid
input `{"items": [out.value for out in outputs]}` and returns the team's `result.text`.

### `Aggregator`

`class Aggregator(Node)` — the fan-in node: consumes a group of N Outputs and emits
one. `kind` is `NodeKind.AGGREGATOR`.

**Constructor** — `Aggregator(reducer, *, output_schema=None, name="aggregator")`:

| Parameter | Type | Default | Notes |
| --- | --- | --- | --- |
| `reducer` | `Reducer` | — (required) | A built-in or `definition_reducer`. |
| `output_schema` | `list[Parameter] \| None` | `None` | Declared shape of the reduced value; stored as `[]` when `None`. |
| `name` | `str` | `"aggregator"` | Node name. |

**Attributes set in `__init__`:** `id` (a fresh `new_id()`), `name`, `kind`
(`NodeKind.AGGREGATOR`), `reducer`, `output_schema` (`list[Parameter]`).

**Method:**

```python
async def reduce(
    self, outputs: list[Output[JSONValue]], ctx: RunContext
) -> Output[JSONValue]
```

Apply `self.reducer` to the N inputs (awaiting it if it returns an awaitable) and emit
one fresh `Output` with `value=reduced`, `output_schema=list(self.output_schema)`, and
`produced_by=self.id`. Never mutates an input.

### `fan_in`

```python
async def fan_in(
    runs_or_coros: list[Awaitable[Output[JSONValue] | None]],
    *,
    quorum: int | None = None,
) -> list[Output[JSONValue]]
```

Barrier that awaits N concurrent runs and returns their successful Outputs. Results that
**raise** or resolve to **`None`** are dropped (partial-success aware); survivors keep
submission order. If `quorum` is given and fewer than `quorum` survive, raises
`ValueError("fan-in quorum not met: ...")`.

---

## Example

The four built-in reducers over a small in-memory list of Outputs — all pure, no agent
or network. The `RunContext` is required by the reducer signature but the built-ins
ignore it.

```python
from crawfish.store.sqlite import SqliteStore
from crawfish.core.context import RunContext
from crawfish.output import Output
from crawfish.nodes.aggregator import collect, concat, count, dedupe

ctx = RunContext(store=SqliteStore(":memory:"))
# Five per-item results, with two repeats.
outs = [Output(output_schema=[], value=v, produced_by="n")
        for v in ["a", "b", "a", "b", "c"]]

print("collect:", collect(outs, ctx))   # gather all, in order
print("concat: ", concat(outs, ctx))    # join as one string, no separator
print("count:  ", count(outs, ctx))     # how many items
print("dedupe: ", dedupe(outs, ctx))    # drop repeats, first-seen order
```

??? success "▶ Output"

    ```text
    collect: ['a', 'b', 'a', 'b', 'c']
    concat:  ababc
    count:   5
    dedupe:  ['a', 'b', 'c']
    ```
