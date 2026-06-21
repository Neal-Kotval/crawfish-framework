# Context carry

The typed state an agent hands to the next one, and the rules that decide how much of
it travels. A `Context` is the accumulated result of a run so far; a
`ContextCarryStrategy` trims it down before the next agent reads it. These live in
`crawfish.runtime` and replace the old habit of stuffing one agent's output into the
next agent's prompt as raw text.

**Symbols on this page:** `Context` · `ContextEntry` · `ContextCarryStrategy` ·
`CarryFull` · `CarryRecency` · `CarrySummary` · `CarryTypedFields` ·
`resolve_carry_strategy`

---

## Core

When several agents work in sequence — a scout finds the PRs, a triager judges them, a
labeler tags them — each one produces a result the next one needs to see. That
accumulated handoff is a **Context**.

A Context is a list of **entries**. Each `ContextEntry` is one named result: its `key`
(how the next agent addresses it, e.g. `"scout_result"`), the `role` that produced it,
and the actual typed `value` — a number, a list, a record, whatever the agent emitted,
kept as data and not flattened into a string.

Two facts ride along on every entry and matter more than they look:

- **`tainted`** — whether this value is *untrusted*. A value is untrusted when it came
  from outside your control (a ticket body, a PR diff — anything fluid). The framework
  tracks this so untrusted data reaching the next agent arrives as *data to read*, never
  as *instructions to obey*. (This is the prompt-injection boundary the
  [security spine](../architecture/SECURITY.md) enforces.)
- **`lineage`** — a short note of where the value came from, so a result can be traced
  back to its producer.

A Context is **frozen**: you never mutate one. Adding an entry returns a *new* Context
with the entry appended. The original is unchanged.

The next question is *how much* of the Context the next agent should receive. Carrying
everything is safe but can be wasteful; sometimes a downstream agent needs only the last
two results, or only two specific fields. A **`ContextCarryStrategy`** answers that. It
takes a Context and returns a (possibly smaller) Context. The four built-ins:

- **`CarryFull`** — keep every entry. The default; nothing is dropped.
- **`CarryRecency`** — keep only the *N* most recent entries, drop the oldest.
- **`CarryTypedFields`** — keep only entries whose `key` is in an allow-list, drop the rest.
- **`CarrySummary`** — collapse all entries into one `summary` entry holding a digest of them.

`resolve_carry_strategy` turns a strategy *name* (a string like `"recency"`, or `None`
for the default) into a ready-to-use strategy object.

---

## Ramps up

### Why a typed Context instead of raw strings

Earlier, one agent's result was threaded to the next as a raw string — the value stuffed
into the next prompt as text. That loses three things: the **type** (a list of records
collapses to text), the **lineage** (no record of where it came from), and the **taint**
(no way to mark it untrusted). `Context`/`ContextEntry` carry all three. A fluid-derived
result stays `tainted` as it crosses into the next agent, so the static/fluid
prompt-injection boundary holds end to end.

### Entries are immutable; Context derivation is copy-on-write

Both `ContextEntry` and `Context` are Pydantic models with `frozen=True` — assigning to a
field raises. `Context.add(entry)` does not append in place; it returns a fresh Context
via `model_copy`. Every carry strategy follows the same rule: it builds the reduced entry
list and returns a new Context, never touching the input. So applying a strategy is safe
to do anywhere and can never corrupt the Context an earlier agent still holds.

### Taint survives reduction

A strategy may drop entries, but it can never *launder* one. This matters most for
`CarrySummary`: if **any** collapsed entry was tainted, the resulting summary entry is
tainted. Compacting untrusted data never quietly turns it trusted. `CarrySummary` also
preserves `lineage` only when every collapsed entry shares the same lineage — once a
summary mixes sources it is no longer single-source, so lineage is dropped to `None`.
`CarryRecency` and `CarryTypedFields` keep their surviving entries verbatim, taint and
lineage untouched.

### No-op short-circuits

Each reducing strategy returns the Context unchanged when reduction would be pointless:
`CarryRecency` when there are `keep` entries or fewer; `CarryTypedFields` when its
allow-list is empty (an empty allow-list means "no projection", not "drop everything");
`CarrySummary` when there is one entry or fewer (nothing to collapse). `CarryFull` is
always a no-op by definition.

### The carry strategies vs. the transcript strategies

The same module (`crawfish.runtime.context_strategy`) also holds a separate
`ContextStrategy` family (`MaxTokens`, `LinearCompact`, …) resolved by `resolve_strategy`.
Those shrink a *single agent's transcript* (its conversation turns) to fit the context
window. The carry strategies here shrink the *cross-agent Context artifact* threaded
between agents. Different inputs, different registries, different resolver — don't confuse
`resolve_carry_strategy` with `resolve_strategy`.

### Strategies are model-free and deterministic

Every built-in carry strategy is pure structural logic — no model call, no clock, no
randomness. `CarrySummary`'s "summary" is a deterministic digest (`{key: value}`), not an
LLM summarization. A model-backed summarizer is left as a later extension; today the
output is fully reproducible.

---

## API reference

### `ContextEntry`

`class ContextEntry(BaseModel)` — one typed value carried between agents. Frozen
(`model_config = {"frozen": True}`).

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `key` | `str` | — (required) | How the next agent addresses this value, e.g. `"scout_result"`. |
| `role` | `str` | — (required) | The agent role that produced the value. |
| `value` | `JSONValue` | `None` | The inline typed value. Left `None` when offloaded to a `ref`. |
| `value_schema` | `list[Parameter]` | `[]` | The value's declared port schema. |
| `ref` | `ArtifactRef \| None` | `None` | Set iff the value is offloaded to an `ArtifactStore` (large payloads). |
| `tainted` | `bool` | `False` | Untrusted / fluid-derived — the injection boundary. |
| `lineage` | `str \| None` | `None` | Where the value came from. |

`is_ref` (property) → `bool`: `True` iff `ref` is set (the value is offloaded and needs
hydration).

### `Context`

`class Context(BaseModel)` — the typed, taint-aware artifact threaded between agents.
Frozen.

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `id` | `str` | `new_id()` | Fresh opaque id (UUID4 string) per Context. |
| `entries` | `list[ContextEntry]` | `[]` | The carried entries, in production order. |

Key methods (all copy-on-write; returns are fresh Contexts):

```python
def add(self, entry: ContextEntry) -> Context
def add_result(self, *, key: str, role: str, result: Output[JSONValue]) -> Context
def to_inputs(self) -> dict[str, JSONValue]
def offload_large(self, store: ArtifactStore, *, org_id: str = "local",
                  threshold: int = ARTIFACT_THRESHOLD_BYTES) -> Context
def hydrate(self, store: ArtifactStore, *, org_id: str = "local") -> Context
def persist(self, store: Store, *, org_id: str = "local") -> None
```

- `add` — append one entry, return a fresh Context.
- `add_result` — carry an agent's typed `Output` forward; taint and lineage propagate from it.
- `to_inputs` — render entries as `{key: value}` for the next agent. A still-offloaded
  entry yields its ref dict, so callers never silently get `None`.
- `tainted` (property) → `bool`: `True` iff any entry is tainted.
- `offload_large` / `hydrate` — move oversized values to / from an `ArtifactStore`;
  `hydrate` is the single deref point ([ADR 0013](../architecture/decisions/0013-emission-taxonomy-and-inline-output-value.md)). Inline by
  default; an entry offloads only when its serialized value exceeds `threshold`
  (`ARTIFACT_THRESHOLD_BYTES`, 32 768 bytes).
- `persist` / `load` (classmethod) — round-trip through the `Store` seam; a
  `ScrubbingStore` redacts secrets so they are never embedded in the artifact.

### `ContextCarryStrategy`

`class ContextCarryStrategy(ABC)` — the abstract base deciding which Context entries the
next agent receives. Deterministic.

```python
class ContextCarryStrategy(ABC):
    name: str = "abstract"

    @abstractmethod
    def carry(self, context: Context) -> Context: ...
```

### `CarryFull`

`class CarryFull(ContextCarryStrategy)` — `name = "full"`. `carry` returns the Context
unchanged (no reduction). The safe default.

### `CarryRecency`

`class CarryRecency(ContextCarryStrategy)` — `name = "recency"`.

```python
def __init__(self, keep: int = 3) -> None
```

`carry` keeps the last `keep` entries (drops oldest). Returns the Context unchanged when
it has `keep` entries or fewer.

### `CarryTypedFields`

`class CarryTypedFields(ContextCarryStrategy)` — `name = "typed_fields"`.

```python
def __init__(self, fields: list[str] | None = None) -> None
```

`carry` keeps only entries whose `key` is in `fields` (taint/lineage/types on kept
entries untouched). An **empty** `fields` is treated as "no projection" — the Context is
returned unchanged, not emptied.

### `CarrySummary`

`class CarrySummary(ContextCarryStrategy)` — `name = "summary"`. `carry` collapses all
entries into a single entry `key="summary"`, `role="system"`, `value={key: value}`
digest. Returns the Context unchanged when it has one entry or fewer. The summary is
`tainted` iff any collapsed entry was tainted; `lineage` is preserved only when all
collapsed entries share one lineage, else `None`.

### `resolve_carry_strategy`

```python
def resolve_carry_strategy(name: str | None) -> ContextCarryStrategy
```

Map a strategy name to a fresh strategy instance. Known names: `"full"`, `"recency"`,
`"typed_fields"`, `"summary"`. `None` resolves to the default `DEFAULT_CARRY_STRATEGY`
(`"full"` — lossless; opt into reduction explicitly). An unknown name raises `KeyError`
listing the known names. Strategies are constructed with default arguments, so to set
`keep` or `fields` you instantiate the class directly rather than going through the
resolver.

---

## Example

Build a Context with three results — one of them untrusted — then apply each strategy and
print which entries survive. Pure structural logic, no runtime needed.

```python
from crawfish.runtime.context_artifact import Context, ContextEntry
from crawfish.runtime.context_strategy import (
    CarryFull, CarryRecency, CarryTypedFields, CarrySummary, resolve_carry_strategy,
)

ctx = Context(
    id="ctx-fixed",
    entries=[
        ContextEntry(key="scout_result",  role="scout",   value={"prs": 12}),
        ContextEntry(key="triage_result", role="triage",  value="needs-review", tainted=True),
        ContextEntry(key="label_result",  role="labeler", value=["bug"]),
    ],
)

def keys(c): return [e.key for e in c.entries]

print("full    ", keys(CarryFull().carry(ctx)))
print("recency2", keys(CarryRecency(keep=2).carry(ctx)))
print("fields  ", keys(CarryTypedFields(fields=["scout_result", "label_result"]).carry(ctx)))

summ = CarrySummary().carry(ctx)
print("summary ", keys(summ), "tainted=", summ.tainted)   # taint survives the collapse
print("digest  ", summ.entries[0].value)

print("resolve ", resolve_carry_strategy(None).name, resolve_carry_strategy("recency").name)
```

??? success "▶ Output"

    ```text
    full     ['scout_result', 'triage_result', 'label_result']
    recency2 ['triage_result', 'label_result']
    fields   ['scout_result', 'label_result']
    summary  ['summary'] tainted= True
    digest   {'scout_result': {'prs': 12}, 'triage_result': 'needs-review', 'label_result': ['bug']}
    resolve  full recency
    ```
