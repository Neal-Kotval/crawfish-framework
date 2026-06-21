# Core types

The connective tissue every other primitive imports: how data is typed, what counts
as a node, and how rule bundles travel. These live in `crawfish.core` and are
deliberately thin and stable — nothing here knows about a specific node, runtime, or
backend.

**Symbols on this page:** `Flow` · `Parameter` · `NodeKind` · `Node` · `PolicyKind` ·
`Policy` · `parameters_compatible` · `new_id` · `JSONValue`

---

## Core

A **pipeline** in Crawfish is a chain of steps — a source pulls in data, a batch fans
it out to an agent, an aggregator reduces the results, and so on. Each step is a
**node**, and nodes are wired together by matching what one *emits* to what the next
one *accepts*.

A **parameter** describes one slot of data crossing that boundary: its `name`, its
`type` (a string like `"str"` or `"list[PR]"`), and whether it is required. Two
parameters wire together when the producer's type can flow into the consumer's type —
that check is `parameters_compatible`.

The single most important field on a parameter is its **flow**:

- **Static** — set once at the start of a batch and the same for every item (a repo
  link, a target board).
- **Fluid** — changes per item as data streams through (a ticket body, a PR diff).

Fluid is more than a label. It is the **prompt-injection boundary**: fluid values are
*untrusted session data*. They reach the model as data to read, never as instructions
to obey. (The [security spine](../architecture/SECURITY.md) enforces this; consequential
sink targets and idempotency keys are static-only for the same reason.) When in doubt,
data that came from outside your control is fluid.

A **policy** is a named, importable bundle of rules — spend caps and content limits
(*guardrails*), which model runs when (*routing*), or what an agent may touch
(*permissions*). Policies are data you attach to a pipeline, not code.

`new_id` hands out a fresh opaque identifier for any object, and `JSONValue` is the
type name for "any JSON-serialisable value" — both small shared helpers used throughout.

---

## Ramps up

### Why parameters carry a string `type`

`Parameter.type` is a **string name**, not a Python type object. That is intentional:
the desktop console and the unit registry need to read a node's port shapes *without
importing Python*. The string is resolved against the structural
[type system](type-system.md) — never by string equality — so `"list[PR]"` and a
record with the right fields compare structurally. See
[ADR 0002](../architecture/decisions/0002-structural-type-registry.md) for the rationale.

### How `parameters_compatible` decides

A value flows producer → consumer, so the check is directional:

```text
parameters_compatible(out, in_)  ==  registry.is_compatible(out.type, in_.type)
```

It resolves both type strings through the registry (the process-wide
[`default_registry`](type-system.md#default_registry) unless you pass your own) and
asks whether a value of the output type can satisfy the input type. A required input
*must* receive a compatible value; an optional or defaulted input may go unfilled. The
function answers only the type question — requiredness is enforced where bindings are
applied, not here.

### `Node` is an ABC, not a model

`Node` is an abstract base class because nodes carry **behaviour**, not just data —
contrast `Parameter`/`Policy`, which are Pydantic `BaseModel`s holding pure data. This
is the project-wide convention: *Pydantic for data shapes, ABCs for behavioural nodes*
([ADR 0004](../architecture/decisions/0004-pydantic-data-abc-behavior.md)). Concrete nodes (a `Source`, an `Aggregator`)
set `id`, `name`, and `kind` in their `__init__`; the kinds themselves are fixed by
`NodeKind`.

### Enums are `(str, Enum)`

`Flow`, `NodeKind`, and `PolicyKind` all subclass `(str, Enum)`. The member's value
*is* the string (`Flow.FLUID == "fluid"`), which lets Pydantic coerce raw strings into
enum members at the boundary and serialise them back without ceremony. The Ruff rule
`UP042` that would flag this is intentionally disabled project-wide.

---

## API reference

### `Flow`

`class Flow(str, Enum)` — whether a parameter is set once per batch or varies per item.

| Member | Value | Meaning |
| --- | --- | --- |
| `Flow.STATIC` | `"static"` | Set once at batch start (e.g. a repo link). |
| `Flow.FLUID` | `"fluid"` | Changes per item as data streams (e.g. a ticket body). **The prompt-injection boundary** — reaches the model as data, never instructions. |

### `Parameter`

`class Parameter(BaseModel)` — a typed parameter on an input/output boundary.

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `name` | `str` | — (required) | Port name. |
| `type` | `str` | — (required) | Type name resolved via the [type registry](type-system.md), e.g. `"str"`, `"list[PR]"`. |
| `required` | `bool` | `True` | A required input must be filled with a compatible value. |
| `default` | `JSONValue \| None` | `None` | Value used when an optional input is unfilled. |
| `flow` | `Flow` | `Flow.FLUID` | Static vs fluid. **Defaults to fluid** — i.e. untrusted unless declared static. |

### `NodeKind`

`class NodeKind(str, Enum)` — the fixed set of node roles:
`SOURCE`, `BATCH`, `SINK`, `FILTER`, `AGGREGATOR`, `ROUTER` (values are the lowercase names).

### `Node`

`class Node(ABC)` — the abstract base anything in a pipeline implements. Attributes set
by concrete subclasses: `id: str`, `name: str`, `kind: NodeKind`. Carries behaviour, so
it is an ABC rather than a model.

### `PolicyKind`

`class PolicyKind(str, Enum)`:

| Member | Value | Governs |
| --- | --- | --- |
| `PolicyKind.GUARDRAIL` | `"guardrail"` | What an agent may do — spend caps, content. |
| `PolicyKind.ROUTING` | `"routing"` | Which model runs under which conditions. |
| `PolicyKind.PERMISSION` | `"permission"` | Which sources/sinks/data an agent may touch. |

### `Policy`

`class Policy(BaseModel)` — an importable rule bundle.

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `name` | `str` | — (required) | Policy name. |
| `kind` | `PolicyKind` | — (required) | Which class of rule this bundles. |
| `rules` | `dict[str, JSONValue]` | `{}` | The rule payload; shape depends on `kind`. |

### `parameters_compatible`

```python
def parameters_compatible(
    out: Parameter,
    in_: Parameter,
    registry: TypeRegistry | None = None,
) -> bool
```

`True` if a value produced at `out` can wire into the input `in_`. Checks `out.type`
against `in_.type` structurally, in the producer → consumer direction, through
`registry` (defaults to `default_registry`).

### `new_id`

`def new_id() -> str` — a fresh opaque identifier (a UUID4 string, 36 chars) for any
framework object.

### `JSONValue`

`JSONValue = Any` — the type alias for a JSON-serialisable value. Kept as `Any` because
Pydantic validates concrete shapes at the boundaries (`Parameter.type` carries the real
type information).

---

## Example

Wiring two ports, building a guardrail policy, and reading a fluid default — all pure,
no runtime needed.

```python
from crawfish import Parameter, Flow, parameters_compatible, Policy, PolicyKind, new_id

# An output that emits a list of PRs, and an input that wants them.
pr_list   = Parameter(name="prs",   type="list[PR]", flow=Flow.FLUID)
wants_prs = Parameter(name="items", type="list[PR]", required=True)
print(parameters_compatible(pr_list, wants_prs))   # structurally compatible

# A bare string cannot satisfy an input that wants a list.
text = Parameter(name="body", type="str")
print(parameters_compatible(text, wants_prs))

# A policy is just named, typed data you attach to a pipeline.
cap = Policy(name="spend-cap", kind=PolicyKind.GUARDRAIL, rules={"max_usd": 5})
print(cap.kind.value, cap.rules["max_usd"])

# Parameters default to fluid (untrusted); ids are opaque UUID4 strings.
print(Parameter(name="x", type="str").flow.value)
print(len(new_id()))
```

??? success "▶ Output"

    ```text
    True
    False
    guardrail 5
    fluid
    36
    ```
