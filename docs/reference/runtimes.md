# Runtimes

The swappable backend that actually runs an agent's turn. Everything above this layer
describes *what* an agent should do; a runtime decides *how* it executes — via the local
`claude -p` CLI, a hosted API, a recorded cassette, or a deterministic mock. These live
in `crawfish.runtime` and are the one place the model SDK/CLI is touched.

**Symbols on this page:** `AgentRuntime` · `RunRequest` · `RunResult` · `RuntimeEvent` ·
`CommandRuntime` · `MockRuntime` · `ClientRuntime` · `ManagedRuntime` ·
`RecordReplayRuntime` · `RoutingRuntime` · `ProviderRuntime` · `ProviderFailover` ·
`expand_candidates` · `get_runtime`

---

## Core

A **runtime** executes one agent's turn. You hand it a request describing which agent to
run and the data to feed it; it runs the agent loop to completion and hands back a typed
result. `AgentRuntime` is the abstract contract for that; concrete runtimes are
interchangeable implementations of it.

This is the key seam in Crawfish. The product model — the nodes, the pipeline, the
team — never imports a specific model backend. It only ever talks to the `AgentRuntime`
interface. Swapping the local CLI for a hosted API, or for a fake used in tests, is a
matter of constructing a different runtime; no node code changes. That is one of the
**three swappable seams** the architecture is built on ([ADR 0001](../architecture/decisions/0001-three-swappable-seams.md)).

Two small data shapes cross the interface:

- A **`RunRequest`** is the input: a compiled **Definition** (an agent-team package — see
  [Definition](definition.md)) plus the `inputs` bound for this run, and optional knobs
  for which agent `role` to run, which `model` to pin, and a `session_id` to resume.
- A **`RunResult`** is the output: the agent's final `text`, the `cost_usd` it incurred,
  the `model` that answered, the resumable `session_id`, and the list of `events` that
  occurred along the way.

A **`RuntimeEvent`** is one step inside the turn — a chunk of model text, a tool call, a
tool result, the final result, or an error. A runtime can stream these as they happen;
by default it just replays them after the turn finishes.

The concrete runtimes you pick from:

- **`MockRuntime`** — a pure function of the request. No model call, zero cost, fully
  deterministic. This is what `craw dev` and every doc example here use.
- **`CommandRuntime`** — the zero-key reference backend. Shells out to `claude -p`, so
  `pip install crawfish` plus the Claude CLI runs a real pipeline with nothing hosted.
- **`ClientRuntime`** / **`ManagedRuntime`** — stubs for the hosted-API and managed-cloud
  backends that land in later phases.
- **`RecordReplayRuntime`** — wraps any runtime and replays a saved transcript (a
  *cassette*) instead of calling the model again.
- **`ProviderRuntime`** — runs across a list of model providers, failing over to the next
  when one is denied by policy or errors.
- **`RoutingRuntime`** — picks which model a step should run *before* delegating to an
  inner runtime.

`get_runtime` is the selector: given a resolved profile (`dev`, `prod`, …) it constructs
the runtime that profile names. Switching profile is a runtime swap, not a code change.

---

## Ramps up

### `AgentRuntime` is an ABC with one required method

`AgentRuntime` is an abstract base class — it carries behaviour, so it is an ABC, not a
Pydantic model (the project-wide convention; see [core types](core-types.md)). The only
method a subclass *must* implement is `run`: execute one agent turn and return a
`RunResult`. `stream` has a default — run to completion, then yield the result's events
one by one — so a runtime that has no real streaming still satisfies the interface. A
class-level `name` string identifies the runtime in telemetry.

The base class also provides `_emit_telemetry`, a static helper every concrete runtime
calls after a run. It writes a compact summary (model, cost, event count, session id,
runtime name) to the Store's event ledger through the typed emission layer — so
observability is written once, the same way, regardless of which backend answered.

### Why the model SDK lives only here

`crawfish.runtime` is the **only** place the model CLI or SDK is touched. Nodes import the
`AgentRuntime` protocol, never a concrete backend ([ADR 0001](../architecture/decisions/0001-three-swappable-seams.md)).
That is what makes dev→prod a runtime swap: `CommandRuntime` (`claude -p`, zero key) →
`ClientRuntime` (API key) → `ManagedRuntime` (managed cloud) are interchangeable behind the
one interface.

### The model type stays universal, the runtime ships Claude-first

A `RunRequest.model` (and an agent's pinned model) is a plain string and may be *any*
model id — the type system makes no vendor assumption. But the default backend resolves
unpinned agents to a Claude model (`CommandRuntime.DEFAULT_MODEL` is `"claude-opus-4-8"`),
and `claude -p` is the reference loop. This is the **claude-first, model-universal**
stance: Claude is the default and best-supported path, but pinning another model is a
config change, not a code change ([ADR 0005](../architecture/decisions/0005-claude-first-universal-model-type.md)). The provider
layer hardcodes *no* vendor default — `ClientRuntime`'s placeholder is the literal
`"unset"` until a `ModelsConfig` or agent model supplies one.

### Determinism: how examples and tests avoid live calls

Three runtimes give you live-call-free runs:

- **`MockRuntime`** is a pure function of the request. Its default responder returns
  `[{role}] processed: {fluid inputs as sorted JSON}` — note it includes only the
  **fluid** inputs (untrusted per-item data), never the **static** ones (trusted
  per-batch config), mirroring the prompt-injection boundary. You can pass your own
  `responder` to return any canned string. Cost is always `0.0`.
- **`CommandRuntime`** takes an injected `transport` — the callable that would spawn the
  subprocess. Tests inject one that returns canned `stream-json` text, so the parser runs
  with no actual `claude -p` process.
- **`RecordReplayRuntime`** replays a saved `RunResult` from a cassette file keyed by a
  hash of the request. A replay charges **zero cost** and makes no model call; a cache
  miss either records (if `record=True`) or raises `CassetteMiss`.

### Failover: candidates, policy, and `ProviderFailover`

`ProviderRuntime` wraps one or more `Provider` backends and fails over across a list of
candidate models. For a request it builds an ordered candidate list with
`expand_candidates` (alias-expanding friendly names to concrete ids, de-duplicated). Then
for each candidate it walks the providers in registration order and asks the **first** one
that is (a) *permitted* by the active `ProviderPolicy` and (b) `supports` that model to
run it. The first success returns; cost and telemetry are charged once for whoever
answered.

Two failure modes differ deliberately. A provider raising `NotImplementedError` — an
unwired stub, e.g. a `ClientProvider` with no injected caller — is a **configuration**
error and propagates immediately rather than failing over. Any other exception is treated
as a transient backend failure and the loop tries the next provider. If every candidate is
exhausted, `ProviderFailover` is raised, carrying the `(model, reason)` pairs so you can
see why each was skipped or failed.

### Routing happens once, upstream

`RoutingRuntime` applies a `RoutingPolicy` to decide which model a step runs, **pins that
model on the request**, then hands off to an inner runtime. It does not re-resolve the
model itself — pinning an already-resolved concrete id means the inner runtime's own model
resolution is a no-op pass-through. An explicit per-run `request.model` override wins over
routing untouched. The decision is made once, through the same shared resolver the cost
estimator uses, so a run can't drift from its preview.

### `get_runtime` and profile selection

`get_runtime` looks up a profile's `runtime` name in `RUNTIME_FACTORIES`
(`"command"`, `"mock"`, `"client"`, `"managed"`) and constructs it. An unknown name
raises `KeyError`. If a `ModelsConfig` is passed *and* the factory is `CommandRuntime`, the
config is forwarded so unpinned agents resolve to `config.default` instead of the built-in
`DEFAULT_MODEL`; other runtimes are constructed unchanged.

---

## API reference

### `AgentRuntime`

`class AgentRuntime(ABC)` — the swappable agent-loop backend. Class attribute
`name: str = "abstract"`.

```python
@abstractmethod
async def run(self, request: RunRequest, ctx: RunContext) -> RunResult
```

Execute one agent turn to completion and return the typed result. The single method a
subclass must implement.

```python
async def stream(
    self, request: RunRequest, ctx: RunContext
) -> AsyncIterator[RuntimeEvent]
```

Stream events. Default implementation: call `run`, then yield each event of the result.

```python
@staticmethod
def _emit_telemetry(ctx: RunContext, result: RunResult, runtime: str) -> None
```

Persist a compact run summary (model, cost, event count, session id, runtime name) to the
Store's event ledger via the typed emission layer.

### `RunRequest`

`class RunRequest(BaseModel)` — one agent's turn: a compiled Definition plus the inputs
bound for this run. (`model_config` allows arbitrary types so it can hold a `Definition`.)

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `definition` | `Definition` | — (required) | The compiled agent-team package to run. |
| `inputs` | `dict[str, JSONValue]` | `{}` | Inputs bound for this run, keyed by parameter name. |
| `role` | `str \| None` | `None` | Which agent to run. `None` → the team lead, else the first agent. |
| `model` | `str \| None` | `None` | Per-run model override; pins a single model and bypasses routing/agent default. |
| `session_id` | `str \| None` | `None` | Resume an existing session. |

### `RunResult`

`class RunResult(BaseModel)` — the typed outcome of a turn.

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `text` | `str` | `""` | The agent's final text. |
| `session_id` | `str \| None` | `None` | Resumable session id. |
| `cost_usd` | `float` | `0.0` | Dollar cost this turn incurred. |
| `model` | `str` | `""` | The model id that answered. |
| `events` | `list[RuntimeEvent]` | `[]` | The events that occurred during the turn. |

### `RuntimeEvent`

`class RuntimeEvent(BaseModel)` — one step inside a turn.

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `kind` | `EventKind` | — (required) | The kind of step (see below). |
| `text` | `str` | `""` | Text payload (model text, tool result, or final result). |
| `tool` | `ToolCall \| None` | `None` | Set on a `TOOL_USE` event. |
| `cost_usd` | `float` | `0.0` | Cost attributed to this event. |
| `session_id` | `str \| None` | `None` | Session this event belongs to. |

`EventKind` is `class EventKind(str, Enum)`:

| Member | Value | Meaning |
| --- | --- | --- |
| `EventKind.TEXT` | `"text"` | A chunk of model text. |
| `EventKind.TOOL_USE` | `"tool_use"` | The agent invoked a tool (carries a `ToolCall`). |
| `EventKind.TOOL_RESULT` | `"tool_result"` | A tool returned a result. |
| `EventKind.RESULT` | `"result"` | The final turn result (carries `text` + `cost_usd`). |
| `EventKind.ERROR` | `"error"` | An error occurred. |

`ToolCall` is `class ToolCall(BaseModel)` with `id: str` (default: a fresh `new_id()`),
`name: str`, and `input: dict[str, JSONValue]` (default `{}`).

### `CommandRuntime`

`class CommandRuntime(AgentRuntime)` — `name = "command"`. The zero-key reference backend
via `claude -p`.

```python
def __init__(
    self,
    *,
    claude_bin: str = "claude",
    transport: Transport | None = None,
    default_model: str = DEFAULT_MODEL,   # "claude-opus-4-8"
    config: ModelsConfig | None = None,
    permission_mode: str | None = None,
) -> None
```

`transport` is the injected subprocess call (`Callable[[list[str], str], Awaitable[str]]`,
returning raw `stream-json` stdout); defaults to spawning `claude_bin`. `run` compiles the
prompt, resolves the model (per-run `request.model` → agent model → `config` → `default_model`),
spawns the transport, parses the `stream-json` output into a `RunResult`, charges the
budget, and emits telemetry.

### `MockRuntime`

`class MockRuntime(AgentRuntime)` — `name = "mock"`. A deterministic, zero-cost backend; a
pure function of the request.

```python
def __init__(self, responder: Responder | None = None) -> None
```

`responder` is `Callable[[RunRequest], str]`; the default returns
`[{role}] processed: {fluid inputs as sorted JSON}`. `run` returns a `RunResult` with the
responder's `text`, `session_id = f"mock-{ctx.run_id}"`, `cost_usd = 0.0`, `model = "mock"`,
and a single `RESULT` event.

### `ClientRuntime`

`class ClientRuntime(AgentRuntime)` — `name = "client"`. API-key backend behind the
provider layer; delegates to a `ProviderRuntime`.

```python
def __init__(
    self,
    *,
    provider_name: str = "client",
    models: list[str] | None = None,
    caller: Caller | None = None,
    default_model: str = "unset",
    config: ModelsConfig | None = None,
    policy: ProviderPolicy | None = None,
) -> None
```

`caller` is the injected egress dependency. While it is `None` (the current default), a run
raises `NotImplementedError` rather than reaching any vendor — no live egress and no
credential read until the sidecar broker lands.

### `ManagedRuntime`

`class ManagedRuntime(AgentRuntime)` — `name = "managed"`. The managed-cloud (CMA) stub.

```python
def __init__(self, *, endpoint: str | None = None) -> None
```

`run` raises `NotImplementedError` — it ships in the managed/cloud phase.

### `RecordReplayRuntime`

`class RecordReplayRuntime(AgentRuntime)` — `name = "replay"`. Deterministic runs from
cassettes.

```python
def __init__(
    self, inner: AgentRuntime, cassette_dir: str | Path, *, record: bool = False
) -> None
```

On a cache hit (a cassette file keyed by a hash of the request exists), replay the recorded
`RunResult` at **zero cost** — no budget charge, no model call. On a miss with
`record=True`, call `inner`, persist the cassette, and return. On a miss with
`record=False`, raise `CassetteMiss`.

### `RoutingRuntime`

`class RoutingRuntime(AgentRuntime)` — `name = "routing"`. Apply a `RoutingPolicy`, pin the
chosen model on the request, then delegate to `inner`.

```python
def __init__(
    self,
    inner: AgentRuntime,
    policy: RoutingPolicy,
    *,
    default_model: str,
    config: ModelsConfig | None = None,
    emit_decision: bool = False,
) -> None
```

An explicit per-run `request.model` override bypasses routing untouched. When
`emit_decision` is set, a `MODEL` emission recording *why* the model was chosen is written
before the inner run.

### `ProviderRuntime`

`class ProviderRuntime(AgentRuntime)` — `name = "provider"`. Fails over across providers,
policy-gated.

```python
def __init__(
    self,
    providers: list[Provider],
    *,
    default_model: str,
    config: ModelsConfig | None = None,
    policy: ProviderPolicy | None = None,
) -> None
```

Requires at least one provider (else `ValueError`). For each candidate model (from
`expand_candidates`), the first provider that is policy-permitted and `supports` the model
runs it; the first success charges cost + telemetry once and returns. Exhausting all
candidates raises `ProviderFailover`. A provider's `NotImplementedError` (an unwired stub)
propagates immediately instead of failing over.

### `ProviderFailover`

`class ProviderFailover(RuntimeError)` — raised when no permitted provider could serve any
candidate.

```python
def __init__(self, attempts: list[tuple[str, str]]) -> None
```

`attempts` is the list of `(model, reason)` pairs explaining each skip/failure; exposed as
the `.attempts` attribute and folded into the message.

### `expand_candidates`

```python
def expand_candidates(
    model: str | list[str] | None,
    *,
    default: str,
    config: ModelsConfig | None = None,
) -> list[str]
```

Alias-expand a `model` field into an ordered failover candidate list. A **list** has
*every* entry alias-expanded (one hop, via `config.aliases`); a `str`/`None`/empty list
collapses to the single resolution from the shared `resolve_model`. Order is preserved and
duplicates dropped (first occurrence wins), so resolution is deterministic.

### `get_runtime`

```python
def get_runtime(
    profile: ProfileConfig, *, config: ModelsConfig | None = None
) -> AgentRuntime
```

Instantiate the runtime named by `profile.runtime` from `RUNTIME_FACTORIES`
(`"command"` → `CommandRuntime`, `"mock"` → `MockRuntime`, `"client"` → `ClientRuntime`,
`"managed"` → `ManagedRuntime`). An unknown name raises `KeyError`. When `config` is given
and the factory is `CommandRuntime`, the config is forwarded so unpinned agents resolve to
`config.default`; other factories are constructed with no arguments.

---

## Example

A deterministic run with `MockRuntime` — no model call, no network, no cost. Note the
result text includes only the **fluid** input (`ticket`), not the **static** one (`repo`):
the mock responder mirrors the prompt-injection boundary.

```python
import asyncio
from crawfish.definition.types import Definition, TeamSpec, AgentSpec
from crawfish.core.types import Parameter, Flow
from crawfish.core.context import RunContext
from crawfish.store import SqliteStore
from crawfish.runtime import MockRuntime
from crawfish.runtime.base import RunRequest

# A one-agent Definition with a static config input and a fluid per-item input.
definition = Definition(
    team=TeamSpec(agents=[AgentSpec(role="triager", prompt="Classify the ticket.")]),
    inputs=[
        Parameter(name="repo", type="str", flow=Flow.STATIC),
        Parameter(name="ticket", type="str", flow=Flow.FLUID),
    ],
)

request = RunRequest(
    definition=definition,
    inputs={"repo": "acme/api", "ticket": "login button is broken"},
)

ctx = RunContext(store=SqliteStore())
result = asyncio.run(MockRuntime().run(request, ctx))

print(result.text)                       # only the fluid input appears
print(result.model)
print(result.cost_usd)
print(len(result.events), result.events[0].kind.value)
print(result.session_id == f"mock-{ctx.run_id}")
```

??? success "▶ Output"

    ```text
    [triager] processed: {"ticket": "login button is broken"}
    mock
    0.0
    1 result
    True
    ```
