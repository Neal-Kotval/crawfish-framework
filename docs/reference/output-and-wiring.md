# Output & wiring

What a node emits and the rules for connecting it to the next node. An `Output`
is the typed envelope of data crossing a node boundary; the wiring checks decide
whether one node's `Output` can feed another node's required inputs.

**Symbols on this page:** `Output` · `output_satisfies_inputs` · `check_wire` · `WireError`

---

## Core

A **node** is one step in a pipeline (a source, a batch, an aggregator). When a node
finishes, it hands the next node an **`Output`**: a small envelope holding the *value*
it produced, the *schema* of that value (a list of [`Parameter`](core-types.md)s naming
and typing each slot), and the *id of the node that produced it*. The downstream node
reads only the `Output` — never the producer's internals.

Two nodes wire together only when the upstream `Output` can supply every input the
downstream node *requires*. That is a **structural check**, not a name-only one: every
required input must be matched by name to a field in the output's schema *and* the two
types must be compatible (see [structural compatibility](type-system.md) and
[`parameters_compatible`](core-types.md#parameters_compatible)).

Two functions answer the wiring question, and they differ only in *how they report*:

- **`output_satisfies_inputs`** returns a plain `bool` — `True` if the wire is valid,
  `False` if not. Use it when you want to test a wire and branch on the result.
- **`check_wire`** returns nothing on success and **raises `WireError`** on failure.
  Use it to *enforce* a wire — to fail loudly at build time rather than silently.

`WireError` is the exception both paths revolve around: it is what `check_wire` throws
when an `Output` cannot satisfy the inputs.

An `Output` is **frozen** — immutable once produced. A node that transforms a value
(a filter, say) does not mutate the upstream `Output`; it calls `derive` to mint a
*fresh* one, leaving the original intact for audit.

---

## Ramps up

### What rides along inside an `Output`

Beyond the value and its schema, an `Output` threads two pieces of provenance:

- **`lineage`** — a stable per-item identity carried unchanged through the pipeline, so
  that idempotency keys (the keys that make a re-run skip work it already did) come out
  the same every time. It is distinct from `id`, which is a fresh UUID minted per
  `Output` instance.
- **`tainted`** — `True` when the value derives from **fluid** input. *Fluid* means
  untrusted session data that streams in per item (a ticket body, a PR diff) — the
  prompt-injection boundary defined in [core types](core-types.md#flow). A tainted value
  must never become a sink target or an idempotency key, per the
  [security spine](../architecture/SECURITY.md).

Both propagate through `derive`: a value derived from a tainted `Output` stays tainted
and keeps the upstream lineage unless you explicitly override them.

### Why `Output` is frozen, and how `derive` works

`Output` sets `model_config = {"frozen": True}`, so any attempt to mutate a field after
construction raises. Transforms therefore use `derive(...)` — a keyword-only method that
copies the current `Output` into a new one with a new `value` and `produced_by`, carrying
`output_schema`, `tainted`, and `lineage` forward unless overridden. Keeping every
intermediate `Output` immutable means the upstream value survives for audit and re-runs
are reproducible.

### How `output_satisfies_inputs` decides

The check is name-matched and required-aware. It indexes the output's schema by parameter
name, then walks each downstream input:

- If the input has **no matching field** in the output schema: it fails only when that
  input is **required and has no default** — an optional or defaulted input may go
  unfilled.
- If there **is** a matching field: the two must pass
  [`parameters_compatible`](core-types.md#parameters_compatible) in the producer →
  consumer direction. An incompatible type fails the wire.

Only required, undefaulted inputs can break a wire by absence; type compatibility is
checked for every name that *does* match. Both checks resolve type strings through the
structural [type registry](type-system.md) ([ADR 0002](../architecture/decisions/0002-structural-type-registry.md)),
never by string equality.

### `WireError` is a `TypeError`

`WireError` subclasses `TypeError`, not bare `Exception` — wiring failures *are* type
errors, so callers that already catch `TypeError` catch these too. `check_wire` is a
thin wrapper: it calls `output_satisfies_inputs` and, on `False`, raises a `WireError`
whose message lists the output's schema field names and the inputs that wanted filling.

---

## API reference

### `Output`

`class Output(BaseModel, Generic[T])` — the unit of data flowing between nodes. Frozen
once produced (`model_config = {"frozen": True}`).

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `id` | `str` | `new_id()` | Fresh UUID4 per `Output` instance. |
| `output_schema` | `list[Parameter]` | `[]` | The shape of `value` — one `Parameter` per slot. |
| `value` | `T` | — (required) | The produced value. |
| `produced_by` | `str` | — (required) | Id of the node that emitted this `Output`. |
| `lineage` | `str \| None` | `None` | Stable per-item identity for deterministic idempotency keys; distinct from `id`. |
| `tainted` | `bool` | `False` | `True` when derived from fluid (untrusted) input. Never allowed as a sink target or idempotency key. |

**Methods:**

```python
def derive(
    self,
    *,
    value: JSONValue,
    produced_by: str,
    output_schema: list[Parameter] | None = None,
    tainted: bool | None = None,
    lineage: str | None = None,
) -> Output[JSONValue]
```

Mint a fresh `Output` from this one (the immutable-derivation path). `tainted` and
`lineage` carry forward from `self` unless explicitly passed; `output_schema` carries
forward when omitted.

```python
def persist(self, store: object, *, org_id: str = "local") -> None
```

Persist this `Output` through the [`Store`](persistence.md) seam as an `"output"` record keyed
by `id`. Typed loosely to avoid an import cycle with `store`.

### `output_satisfies_inputs`

```python
def output_satisfies_inputs(
    output: Output[object],
    inputs: list[Parameter],
    *,
    registry: TypeRegistry | None = None,
) -> bool
```

`True` if `output`'s schema can satisfy every required downstream input. Each input is
matched by name; a matched field must be structurally compatible (producer → consumer);
an unmatched required input with no default fails the wire. `registry` defaults to the
process-wide [`default_registry`](type-system.md#default_registry).

### `check_wire`

```python
def check_wire(
    output: Output[object],
    inputs: list[Parameter],
    *,
    registry: TypeRegistry | None = None,
) -> None
```

Enforcing form of the check: returns `None` when the wire is valid, otherwise raises
`WireError`. Delegates to `output_satisfies_inputs` with the same `registry`.

### `WireError`

`class WireError(TypeError)` — raised when an upstream `Output` cannot wire into a
downstream node's inputs. The message reports the output's schema field names and the
inputs that went unsatisfied.

---

## Example

A compatible wire passes both checks; an incompatible one raises `WireError`. Pure,
no runtime needed — `derive` shows taint propagating while the upstream stays frozen.

```python
from crawfish.output import Output, output_satisfies_inputs, check_wire, WireError
from crawfish import Parameter, Flow

# A node that emits a list of PRs (fluid/untrusted), tagged with its producer id.
emitted = Output(
    output_schema=[Parameter(name="prs", type="list[PR]", flow=Flow.FLUID)],
    value=[{"number": 1}],
    produced_by="source-1",
    tainted=True,
)

# A downstream node that requires a `prs` input.
wants = [Parameter(name="prs", type="list[PR]", required=True)]
print(output_satisfies_inputs(emitted, wants))   # compatible
check_wire(emitted, wants)                        # does not raise
print("wire ok")

# A downstream node that requires a port the output never emits.
needs_author = [Parameter(name="author", type="str", required=True)]
print(output_satisfies_inputs(emitted, needs_author))   # not satisfiable
try:
    check_wire(emitted, needs_author)
except WireError as e:
    print(f"WireError: {e}")

# Taint propagates through derive; the upstream stays frozen.
child = emitted.derive(value=[{"number": 1, "ok": True}], produced_by="filter-1")
print("tainted propagated:", child.tainted)
```

??? success "▶ Output"

    ```text
    True
    wire ok
    False
    WireError: output (schema fields ['prs']) cannot satisfy inputs {'author': 'str'}
    tainted propagated: True
    ```
