# Nodes — router & sink

The two boundary nodes that decide *where data goes*: a **router** branches one
stream into several by attaching a label to each item, and a **sink** is the one place
a pipeline performs an outside-world write (open a PR, post a comment). Both live in
`crawfish.nodes` and both are guarded — a router refuses to assemble if any branch is
missing, and a sink refuses to construct if its destination could be steered by
untrusted data.

**Symbols on this page:** `Router` · `Classifier` · `UnroutableLabelError` · `Sink` ·
`LinearSink` · `GitHubPRSink` · `TargetMustBeStaticError` · `ApprovalRequired`

---

## Core

A **router** takes the single stream of items flowing through a pipeline and splits it
into several **branches** — for example, send bug reports to one downstream node and
feature requests to another. To decide which branch an item takes, the router asks a
**classifier** for a **label** (a short string like `"bug"` or `"feature"`). Each label
maps to one downstream node; that mapping is the router's `branches`.

A **classifier** turns one item into one label drawn from a fixed, closed set you
declare up front. That set always includes a **default** label — the "none of the
above" / dead-letter bucket — so *every* item is guaranteed a destination. There are two
flavours:

- **Predicate** (`Classifier.from_predicates`): an ordered map of `label → test`, where
  each test is a plain function returning `True`/`False`. The first test that passes
  wins; if none pass, the item gets the default. Pure and synchronous — no model call.
- **Definition-backed** (`Classifier.from_definition`): runs an agent team that emits a
  label as free text, which is then matched to one of your allowed labels. Used when the
  decision needs a model.

If a classifier could ever emit a label that the router has no branch for, the pipeline
is broken — there would be an item with nowhere to go. Crawfish catches this when you
*construct* the `Router` (assembly time), not when it runs, by raising
`UnroutableLabelError`. The routing graph is proven total before a single item flows.

A **sink** is the egress boundary — egress meaning data leaving the system to the
outside world: the only node that performs an external side effect.
Two concrete sinks ship — `LinearSink` (create a Linear issue/comment) and
`GitHubPRSink` (open a GitHub pull request). Both default to **dry-run**: instead of
touching the network they record what they *would* have written into a `writes` list, so
tests stay deterministic and offline.

Because a sink writes to the outside world, two safety terms matter:

- **Static-only target.** A sink's *target* is the destination address of a write — the
  Linear team, the GitHub repo. "Static" means the value is fixed once at the start of a
  batch and is identical for every item (the opposite is **fluid** — a per-item value
  that can be influenced by model output or untrusted input). Sink targets must be
  static so that a malicious prompt buried in fluid data can never redirect a write to a
  destination you didn't choose. A fluid target is rejected the moment you construct the
  sink, with `TargetMustBeStaticError`.
- **Idempotency key.** "Idempotent" means doing the same operation twice has the same
  effect as doing it once. A sink derives a unique fingerprint — the *idempotency key* —
  for each write from its static configuration plus the batch and item identity (never
  from the fluid value). The key is claimed atomically before writing, so re-running the
  same batch is a silent no-op rather than a duplicate PR.

Sinks marked `always_ask` add an **approval gate**: they refuse to fire unless a human
approval callback says yes. Asking such a sink to write with no callback raises
`ApprovalRequired`.

---

## Ramps up

This page is part of the [security spine](../architecture/SECURITY.md): the router keeps
the branching graph total, and the sink keeps consequential writes static-targeted,
idempotent, and (optionally) human-gated. No ADR governs these symbols directly; the
load-bearing rule is the SECURITY.md egress contract.

### Why unroutability is an assembly-time error

`Router.__init__` compares the classifier's full `labels` set against the keys of
`branches`. Any label without a branch — and, separately, a `default` that has no branch
— raises `UnroutableLabelError` (a `ValueError`) immediately. This is deliberate: a
routing hole is a structural defect, so it surfaces when you wire the pipeline, never as
a `KeyError` mid-run on the one item that happened to hit the missing branch. Once
constructed, `Router.route` (pure) and `Router.route_async` (agent-backed) can index
`branches[label]` without guarding, because totality is already proven.

### How a predicate classifier picks a label

`from_predicates` preserves the mapping's insertion order and appends `default` to the
label set if it isn't already a key. `classify` walks the predicates in that order and
returns the **first** label whose predicate returns `True` on the item's value, else the
default. Order is therefore significant — earlier labels win when more than one predicate
would match. `classify` raises `TypeError` if called on a Definition-backed classifier
(which has no predicates); use `classify_async` there.

### How an agent label is normalised

A Definition-backed classifier runs its agent team and gets back free text, which
`classify_async` maps to an allowed label by case-insensitive token match: a label is
chosen if it appears as a whitespace-delimited token in the text (so `"the label is
bug."` → `"bug"`), trying labels in declared order, falling back to `default` on no
match. The classification run deliberately skips input-type and output-schema validation
— it over-binds the item into every required slot and reads the run's free text — so it
needs no knowledge of the Definition's port names.

### Why sink targets must be static

A *fluid* value is per-item and can carry model output or untrusted session data. If a
write's destination could be fluid, a prompt-injection payload in an item could redirect
egress — open a PR against an attacker's repo, post to the wrong channel.
`Sink.__init__` walks `target_params` and raises `TargetMustBeStaticError` for any param
whose `flow` is not `Flow.STATIC`. The rejection happens at construction (wire/compile
time), so the guarantee holds before the pipeline ever runs a model.

### Idempotency excludes fluid data by design

`Sink._idempotency_key` hashes (SHA-256) a payload of the sink name, `ctx.batch_id`, the
item's stable `lineage` (falling back to `output.id`), and the JSON of the sink's static
`config` — sorted for order-stability. The `Output` *value* and any model-derived data
are excluded on purpose: a re-run of the same batch/item yields the same key (so the
write no-ops), and a perturbed prompt can't escape idempotency by changing the key. The
key is claimed via `ctx.store.claim_idempotency`; if the claim is lost, the write is
skipped.

### The approval gate fires before the idempotency claim

In `Sink.write`, the `always_ask` check runs *before* claiming the idempotency key. This
ordering matters: a declined write must be retryable later, but an idempotency claim is
permanent — so a decline never burns the claim. With no `approve` callback on an
`always_ask` sink, `write` raises `ApprovalRequired`; a callback returning `False` skips
the write (`write` returns `False`); `True` proceeds to the claim-then-write path.

### Credentials by reference, never by value

Concrete sinks read a credential by **reference** — `config["credential_ref"]` holds the
*name* of an env var, never the secret itself. The recorded write carries that name, so
no secret reaches stored config, the `Output`, logs, or telemetry. On a successful
write, `Sink.write` emits a typed `SINK` telemetry event whose `target` is the static
sink name and whose `tainted` flag propagates from the producing `Output` — never the
credential value or the (possibly model-derived) output value.

---

## API reference

### `UnroutableLabelError`

`class UnroutableLabelError(ValueError)` — raised at assembly time (in `Router.__init__`)
when a classifier label, or the classifier's `default`, has no matching router branch.

### `Classifier`

`class Classifier` — produces one label from a closed set for an `Output`. Construct via
the classmethods, not the raw `__init__`.

```python
@classmethod
def from_predicates(
    predicates: Mapping[str, Callable[[JSONValue], bool]],
    *,
    default: str,
    name: str = "classifier",
) -> Classifier
```

Pure classifier from an ordered `label → predicate` map. The label set is the map's keys
plus `default` (appended if absent), in insertion order.

```python
@classmethod
def from_definition(
    definition: Definition,
    *,
    labels: list[str],
    default: str,
    name: str = "classifier",
) -> Classifier
```

Agent-backed classifier over an explicit `labels` set. `default` must be in `labels`.

| Method | Signature | Behaviour |
| --- | --- | --- |
| `classify` | `(output: Output[JSONValue]) -> str` | First predicate-matched label, else `default`. Raises `TypeError` on a Definition-backed classifier. |
| `classify_async` | `(output, ctx: RunContext, runtime: AgentRuntime) -> str` | Runs the agent team and normalises its text to a label. Short-circuits to `classify` if predicate-backed. |

Attributes: `id: str`, `name: str`, `labels: list[str]`, `default: str`. The raw
`__init__` raises `ValueError` if `default` is not in `labels`.

### `Router`

`class Router(Node)` — routes an `Output` down one labelled branch. `kind` is
`NodeKind.ROUTER`.

```python
def __init__(
    self,
    branches: Mapping[str, Node],
    classifier: Classifier,
    name: str = "router",
) -> None
```

Raises `UnroutableLabelError` if any `classifier.labels` entry — or `classifier.default`
— is missing from `branches`.

| Method | Signature | Returns |
| --- | --- | --- |
| `route` | `(output: Output[JSONValue]) -> tuple[str, Node]` | `(label, branch)` via the pure `classify` path. |
| `route_async` | `(output, ctx: RunContext, runtime: AgentRuntime) -> tuple[str, Node]` | `(label, branch)` via the agent-backed `classify_async` path. |

Attributes: `id: str`, `name: str`, `kind: NodeKind`, `branches: dict[str, Node]`,
`classifier: Classifier`.

### `TargetMustBeStaticError`

`class TargetMustBeStaticError(ValueError)` — raised in `Sink.__init__` when a
`target_params` entry has `flow` other than `Flow.STATIC`. Enforced at construction so a
fluid (per-item, model-influenced) target cannot redirect egress.

### `ApprovalRequired`

`class ApprovalRequired(RuntimeError)` — raised by `Sink.write` when an `always_ask` sink
is invoked without an `approve` callback.

### `Sink`

`class Sink(Node, ABC, Generic[T])` — base egress node. `kind` is `NodeKind.SINK`.
Subclasses implement `_write`; they never reimplement the idempotency or approval
invariants, which live in the public `write`.

```python
def __init__(
    self,
    name: str,
    config: dict[str, JSONValue] | None = None,
    *,
    always_ask: bool = False,
    target_params: list[Parameter] | None = None,
) -> None
```

| Member | Signature | Notes |
| --- | --- | --- |
| `_write` *(abstract)* | `async (output: Output[T], ctx: RunContext) -> None` | The actual side effect; implemented by concrete sinks. |
| `write` | `async (output: Output[T], ctx: RunContext, *, approve: ApproveCallback \| None = None) -> bool` | Invariant-enforcing entry point. `True` if the write ran, `False` if skipped (already written, or approval declined). Raises `ApprovalRequired`. |
| `_idempotency_key` | `(output: Output[T], ctx: RunContext) -> str` | SHA-256 of static config + batch/item identity; excludes the fluid value. |

Attributes: `id`, `name`, `kind`, `config: dict[str, JSONValue]`, `always_ask: bool`,
`target_params: list[Parameter]`. `ApproveCallback = Callable[[], bool]` (returns `True`
to allow the write).

### `LinearSink`

`class LinearSink(Sink[JSONValue])` — create a Linear issue/comment.

```python
def __init__(
    self,
    name: str = "linear",
    config: dict[str, JSONValue] | None = None,
    *,
    always_ask: bool = False,
    target_params: list[Parameter] | None = None,
    dry_run: bool = True,
) -> None
```

In `dry_run` (the default), `_write` appends a record to `self.writes` instead of hitting
the network. Live mode raises `NotImplementedError` (the reference sink is offline-only).
Reads `config["team"]`, `config["project"]`, and `config["credential_ref"]` (the env-var
*name*). Extra attributes: `dry_run: bool`, `writes: list[dict[str, JSONValue]]`.

### `GitHubPRSink`

`class GitHubPRSink(Sink[JSONValue])` — open a GitHub pull request. Same signature as
`LinearSink` except `name` defaults to `"github_pr"`. In `dry_run` (the default) records
to `self.writes`; live mode raises `NotImplementedError`. Reads `config["repo"]`,
`config["base"]`, and `config["credential_ref"]`. Extra attributes: `dry_run: bool`,
`writes: list[dict[str, JSONValue]]`.

---

## Example

A predicate router that branches numbers by sign — printing where each lands — followed
by proof that a fluid sink target is rejected at construction. Pure and in-memory: no
runtime, no network.

```python
from crawfish.core.types import Flow, Node, NodeKind, Parameter
from crawfish.core.ids import new_id
from crawfish.output import Output
from crawfish.nodes.router import Classifier, Router, UnroutableLabelError
from crawfish.nodes.sink import LinearSink, TargetMustBeStaticError


# A trivial terminal node for each branch (just carries a name).
class Land(Node):
    def __init__(self, name: str) -> None:
        self.id = new_id()
        self.name = name
        self.kind = NodeKind.SINK


# Classify a number by sign. `default` (here "zero") is always in the label set.
clf = Classifier.from_predicates(
    {"positive": lambda v: v > 0, "negative": lambda v: v < 0},
    default="zero",
)
print("labels:", clf.labels)

# Every label (incl. the default dead-letter) must have a branch, or assembly fails.
router = Router(
    branches={
        "positive": Land("pos-branch"),
        "negative": Land("neg-branch"),
        "zero": Land("zero-branch"),
    },
    classifier=clf,
)

# Route a few items (pure path — no model call).
for v in (7, -3, 0):
    out = Output(value=v, produced_by="src")
    label, branch = router.route(out)
    print(f"{v!r} -> label={label!r} branch={branch.name!r}")

# Omitting a branch is caught at construction (assembly time), not run time.
try:
    Router(branches={"positive": Land("p"), "zero": Land("z")}, classifier=clf)
except UnroutableLabelError as e:
    print("UnroutableLabelError:", "negative" in str(e))

# A STATIC target is accepted.
ok = LinearSink(
    "linear",
    config={"team": "ENG"},
    target_params=[Parameter(name="team", type="str", flow=Flow.STATIC)],
)
print("static target accepted:", ok.name)

# A FLUID target is rejected at construction — a prompt can't redirect egress.
try:
    LinearSink(
        "linear",
        target_params=[Parameter(name="team", type="str", flow=Flow.FLUID)],
    )
except TargetMustBeStaticError as e:
    print("TargetMustBeStaticError:", "must be" in str(e))
```

??? success "▶ Output"

    ```text
    labels: ['positive', 'negative', 'zero']
    7 -> label='positive' branch='pos-branch'
    -3 -> label='negative' branch='neg-branch'
    0 -> label='zero' branch='zero-branch'
    UnroutableLabelError: True
    static target accepted: linear
    TargetMustBeStaticError: True
    ```
