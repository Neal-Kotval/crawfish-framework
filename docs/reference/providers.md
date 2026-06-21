# Providers

A **provider** is the backend that actually serves a model request — the thing that
turns "run agent X on this input" into text. This page covers the provider contract,
the config that decides *which* model id a request uses, and the three providers that
ship: a deterministic test double and two real-backend adapters. These live in
`crawfish.provider` and `crawfish.runtime`.

**Symbols on this page:** `Provider` · `ProviderPolicy` · `ModelsConfig` ·
`resolve_model` · `MockProvider` · `ClientProvider` · `LocalHTTPProvider` ·
`OpenAIChatRequest` · `LocalTransport`

---

## Core

When a pipeline reaches a step that calls a model, two questions must be answered:
*which model id do we run*, and *who serves it*.

The **which** is `resolve_model`. An agent's `model` field may be unset, a single id, a
friendly alias like `"fast"`, or a list (a failover order). `resolve_model` collapses
all of those to one concrete model id, using a `ModelsConfig` — a small bundle of a
project default plus a name→id alias map. There is no vendor model baked into the
framework; the caller always supplies the ultimate `default`.

The **who** is a `Provider`. A provider is any object that can list the models it
serves, say whether it supports a given model, and run one model turn. It is a
**structural protocol** — Crawfish never asks "is this a subclass of Provider", only
"does this object have the right methods". Three providers ship:

- **`MockProvider`** — a deterministic, in-memory fake. No model call, no network, zero
  cost. Its reply is a pure function of the request, so tests (and the examples in these
  docs) get the same bytes every run. This is what `craw dev` and the whole test suite
  use.
- **`ClientProvider`** — a skeleton adapter for a hosted vendor API (Anthropic, OpenAI,
  Gemini). It deliberately holds **no credential and makes no network call yet**:
  credential handling is gated on later work, so calling it without an injected helper
  raises rather than reaching out.
- **`LocalHTTPProvider`** — talks to a local inference server (llama.cpp's
  `llama-server`, or Ollama) over the OpenAI-compatible HTTP shape. Credential-free —
  there is no API key for a server on your own machine.

A **`ProviderPolicy`** is the guardrail on the *who*: a list of which providers a
pipeline is permitted to use. `allowed=None` means "any provider is fine" (the
local-first default); a list restricts it — a data-residency choice, e.g. "this
pipeline may only ever hit `local`".

The two HTTP-adjacent helpers — **`OpenAIChatRequest`** (the request body a local server
accepts) and **`LocalTransport`** (the one callable that actually sends it) — exist so
`LocalHTTPProvider` holds no transport or network policy of its own.

---

## Ramps up

### One resolver, no hardcoded vendor (ADR 0005)

`resolve_model` is the **single** model resolver in the framework. Model selection once
lived duplicated inside the command runtime and the cost estimator; both now delegate
here, so a cost *preview* can never drift from the model the runtime actually runs.

Per [ADR 0005](../architecture/decisions/0005-claude-first-universal-model-type.md) (claude-first, model-agnostic *by type*), no
vendor model id is hardcoded in this module. Every caller passes its own `default`, so
the framework itself stays vendor-neutral — swapping the default model is config, not a
code change. See [runtimes](runtimes.md) for who calls `resolve_model` and when.

Resolution rules, in order:

```text
None (unpinned)  → config.default if set, else the caller's `default`
"a-model-id"     → itself, after one alias-expansion hop
["a", "b", ...]  → the first entry "a" (primary; the rest are failover), alias-expanded
[]  (empty list) → falls back exactly like None
```

Alias expansion is a **single hop**: an alias must map to a concrete model id, never to
another alias. The whole function is pure and deterministic — same inputs, same id.

### A `Provider` is structural, frozen-shaped behaviour

`Provider` is a `runtime_checkable` `Protocol`, not a base class. Any object exposing a
`name: str`, `models()`, `supports()`, and an async `run()` *is* a provider — the three
shipped providers don't inherit from it, they just match its shape. This keeps the
provider surface frozen while new backends (Anthropic / OpenAI / Gemini / local) can be
added without touching the protocol.

The payoff: observability and cost capture are written once, against the protocol, and
every backend inherits them.

### Fluid inputs stay data, never instructions

Both serving providers treat the request's **fluid** inputs — the untrusted, per-item
session data (a PR body, a ticket) — as data to read, never instructions to obey, which
is the framework's prompt-injection boundary (see the [security spine](../architecture/SECURITY.md)).
`MockProvider` echoes fluid inputs back as a JSON string; `LocalHTTPProvider` runs them
through `compile_prompt`, which fences untrusted data into a delimited block. Neither
ever splices fluid text into the instruction position.

### Why `ClientProvider` refuses to run

`ClientProvider` is a skeleton on purpose. It holds no secret and reads nothing from
`.env`. The thing that would actually reach a vendor API is an **injected `caller`**
that stays `None` until the typed secret schema and credential broker land. With no
caller, `run` raises `NotImplementedError` rather than egressing — so it can never
*silently* reach the network. Onboarding keys before that broker exists would widen the
exact gap it is meant to close.

Note its `supports()` quirk: an **empty** model set is read as "unconfigured stub", and
it claims support for *any* model so the call reaches `run` (and raises loudly) instead
of being silently skipped.

### Why `LocalHTTPProvider` is the cheap leg ([ADR 0011](../architecture/decisions/0011-ruvllm-rvagent-evaluation.md))

ADR 0011 settles the local-model path as a thin adapter — a seed-pinned,
OpenAI-compatible HTTP POST to a local server — rather than a vendored inference engine.
Because it talks to a server on your own machine, it is credential-free and never
touches the deferred cloud-credential path.

Its single egress point is the injected `LocalTransport` callable. In production that is
a stdlib HTTP POST to `localhost`; **in tests a fake transport returns canned JSON and
no real HTTP happens** — the determinism rule. With no transport injected, `run` raises
rather than guessing a network call. Every request carries a pinned `seed` and
`temperature: 0.0` (greedy decoding), so a recorded response replays bit-for-bit. Its
`cost_usd` defaults to `0.0` — local inference burns no metered budget, which is the
whole point of routing cheap steps to it. Its `name` defaults to `"local"`, so a routing
rule (or an agent/alias) targeting `model="local"` lands here.

`_parse_chat_completion` is tolerant of shape drift across llama.cpp / Ollama: it reads
`choices[0].message.content`, falls back to the older `choices[0].text`, and returns
`""` for a malformed/empty body rather than raising — so even a cassette of a degenerate
response still replays.

---

## API reference

### `resolve_model`

```python
def resolve_model(
    model: str | list[str] | None,
    *,
    default: str,
    config: ModelsConfig | None = None,
) -> str
```

The one canonical resolver. Collapses an agent's `model` field to a single concrete
model id. `None`/empty-list → `config.default` if set else `default`; `str` → itself;
non-empty `list` → its first entry. The chosen value is then alias-expanded once through
`config.aliases` (no-op when `config` is `None`).

### `ModelsConfig`

`class ModelsConfig(BaseModel)` — project-level model configuration. **Frozen** (rejects
mutation after construction).

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `default` | `str \| None` | `None` | Fallback model id for unpinned agents. |
| `aliases` | `dict[str, str]` | `{}` | Friendly name → concrete model id; one-hop expansion. |
| `policy` | `ProviderPolicy` | `ProviderPolicy()` | Which providers this config permits. |

### `ProviderPolicy`

`class ProviderPolicy(BaseModel)` — which providers a pipeline may use. **Frozen.**

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `allowed` | `tuple[str, ...] \| None` | `None` | `None` = any provider permitted (local-first default); a tuple restricts failover/routing to the listed providers. |

| Method | Signature | Returns |
| --- | --- | --- |
| `permits` | `permits(self, provider: str) -> bool` | `True` if `allowed is None` or `provider` is in `allowed`. |

### `Provider`

`class Provider(Protocol)` — `@runtime_checkable`. A normalized model backend behind
[`AgentRuntime`](runtimes.md). Structural: any object with these members satisfies it.

| Member | Signature | Meaning |
| --- | --- | --- |
| `name` | `str` (attribute) | The provider's identifier (e.g. `"local"`). |
| `models` | `models(self) -> list[str]` | The concrete model ids this provider can serve. |
| `supports` | `supports(self, model: str) -> bool` | `True` if it can serve `model`. |
| `run` | `async run(self, request: RunRequest, ctx: RunContext) -> RunResult` | Execute one model turn; return the normalized result. |

### `MockProvider`

`class MockProvider` — a deterministic, zero-cost `Provider` for tests and docs.

```python
MockProvider(
    name: str,
    models: list[str],
    *,
    cost_usd: float = 0.0,
    fail: bool = False,
)
```

| Arg | Type | Default | Notes |
| --- | --- | --- | --- |
| `name` | `str` | — (required) | Provider name. |
| `models` | `list[str]` | — (required) | The model ids it serves. |
| `cost_usd` | `float` | `0.0` | Fixed cost reported per turn. |
| `fail` | `bool` | `False` | When `True`, `run` raises `RuntimeError` (drives failover tests). |

`run` checks the cancel token, then returns a `RunResult` whose `text` is
`"[{name}:{role}] {fluid-inputs-as-sorted-json}"` — a pure function of the request's
fluid inputs. `session_id` is `"{name}-{ctx.run_id}"`; `model` is the request's model or
the first served id.

### `ClientProvider`

`class ClientProvider` — a hosted-API adapter skeleton; credential acquisition deferred.

```python
ClientProvider(name: str, models: list[str], *, caller: Caller | None = None)
```

where `Caller = Callable[[RunRequest, RunContext], Awaitable[RunResult]]`. Holds no
secret and performs no network I/O. `supports()` returns `True` for any model when its
model set is empty (the "unconfigured stub" case). `run` raises `NotImplementedError`
unless a `caller` is injected — it never silently egresses.

### `LocalHTTPProvider`

`class LocalHTTPProvider` — a `Provider` over a local OpenAI-compatible server.

```python
LocalHTTPProvider(
    *,
    name: str = "local",
    models: list[str] | None = None,
    transport: LocalTransport | None = None,
    endpoint: str = "http://localhost:8080/v1/chat/completions",
    seed: int = 0,
    cost_usd: float = 0.0,
)
```

| Arg | Type | Default | Notes |
| --- | --- | --- | --- |
| `name` | `str` | `"local"` | The id routing rules target. |
| `models` | `list[str] \| None` | `None` → `["local"]` | Served model ids; `None` defaults to `["local"]`. |
| `transport` | `LocalTransport \| None` | `None` | Injected egress callable; `None` → `run` raises. |
| `endpoint` | `str` | `http://localhost:8080/v1/chat/completions` | The local server path. |
| `seed` | `int` | `0` | Pinned decode seed for reproducibility. |
| `cost_usd` | `float` | `0.0` | Reported per-turn cost (local = unmetered). |

`run` checks the cancel token, builds an `OpenAIChatRequest` via `compile_prompt`
(enforcing the fluid/static boundary), POSTs it through `transport`, and parses the
reply. With no `transport`, it raises `NotImplementedError`.

### `OpenAIChatRequest`

`class OpenAIChatRequest` — the `/v1/chat/completions` body a local server accepts. A
plain value object (no Pydantic — it is a transport detail, not a public contract).

```python
OpenAIChatRequest(*, model: str, prompt: str, seed: int, endpoint: str)
```

| Member | Type | Notes |
| --- | --- | --- |
| `model` | `str` | Model id to run. |
| `prompt` | `str` | The compiled prompt. |
| `seed` | `int` | Pinned decode seed. |
| `endpoint` | `str` | Server path the transport POSTs to. |
| `as_body()` | `-> dict[str, object]` | Renders the JSON dict: `model`, a one-message `messages` list, `seed`, `temperature: 0.0`, `stream: False`. |

### `LocalTransport`

```python
LocalTransport = Callable[[OpenAIChatRequest], Awaitable[str]]
```

A type alias for the single injected egress callable: given an `OpenAIChatRequest`,
return the raw JSON response **text** (a `str`). The production implementation is a
stdlib POST to `localhost`; tests inject a fake that returns canned JSON and performs no
network I/O. Kept narrow so the provider holds no transport policy.

---

## Example

Resolving a model id through a small `ModelsConfig`, gating with a `ProviderPolicy`, and
serving a canned response from a `MockProvider` — all deterministic, no network.

```python
import asyncio
from crawfish.provider import resolve_model, ModelsConfig, ProviderPolicy
from crawfish.runtime.providers import MockProvider
from crawfish.core.context import RunContext
from crawfish.runtime.base import RunRequest
from crawfish.definition.types import AgentSpec, Definition, TeamSpec
from crawfish.store import SqliteStore

# A small config: a project default + one friendly alias.
cfg = ModelsConfig(default="claude-haiku", aliases={"fast": "claude-haiku"})
print(resolve_model(None, default="fallback", config=cfg))    # unpinned -> config.default
print(resolve_model("fast", default="fallback", config=cfg))  # alias expands
print(resolve_model(["x", "y"], default="fallback"))          # list -> first, no config
print(resolve_model(None, default="fallback"))                # no config -> caller default

# A policy gates which providers may serve.
pol = ProviderPolicy(allowed=("anthropic", "local"))
print(pol.permits("local"), pol.permits("openai"), ProviderPolicy().permits("any"))

# A MockProvider: deterministic, zero-cost, canned reply.
mock = MockProvider("mock", ["claude-haiku"], cost_usd=0.0)
print(mock.name, mock.supports("claude-haiku"), mock.supports("gpt"))

d = Definition(team=TeamSpec(agents=[AgentSpec(role="scout", prompt="scan")]))
req = RunRequest(definition=d, role="scout", inputs={"pr_body": "untrusted text"})
ctx = RunContext(store=SqliteStore(), run_id="r1")
res = asyncio.run(mock.run(req, ctx))
print(res.text)                                # fluid input echoed back AS DATA
print(res.cost_usd, res.model, res.session_id)
```

??? success "▶ Output"

    ```text
    claude-haiku
    claude-haiku
    x
    fallback
    True False True
    mock True False
    [mock:scout] {"pr_body": "untrusted text"}
    0.0 claude-haiku mock-r1
    ```
