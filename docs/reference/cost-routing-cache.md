# Cost, routing & cache

Three cost levers that all stay deterministic and never make a live model call:
*preview* the dollar spend of a run before it starts, *route* each step to a cheaper
or stronger model, and *cache* repeated calls so the second one costs nothing. They
live in `crawfish.cost`, `crawfish.routing`, and `crawfish.cache`.

**Symbols on this page:** `estimate_cost` · `CostEstimate` · `Budget` · `BudgetState` ·
`CostMeter` · `spent_today` · `CostTier` · `RoutingRule` · `RoutingPolicy` ·
`RoutingDecision` · `agent_tier` · `route_model` · `route_decision` · `routing_emission` ·
`cache_key` · `CacheStats` · `CachingRuntime`

---

## Core

A **definition** is a compiled agent team — a package of one or more agents authored as
a directory. Each agent names (or leaves unset) the **model** it runs on. Running a
definition over many input **items** costs money, so Crawfish gives you three tools to
keep that spend visible and small.

**Preview the bill.** `estimate_cost` is a dry run: hand it a definition, an item count,
and a price table, and it predicts dollars before a single model call. The estimate is
deliberately coarse — charge one "run" (one agent answering once) per agent per item,
priced from a flat per-model table. The answer comes back as a `CostEstimate`, a frozen
record carrying the per-item cost, the total, and a per-model breakdown so you can see
which model dominates the bill.

**Cap the spend.** A `Budget` is a warn/stop policy: a `stop_usd` hard ceiling and a
`warn_usd` soft line (defaulting to 80% of the stop). Ask it `check(spent)` and it
classifies where you stand as a `BudgetState` — `OK`, `WARN`, or `STOPPED`. A `CostMeter`
is the live accumulator: call `charge(amount)` as runs finish and it tracks running spend
and the headroom left against the budget. To total *yesterday-and-before-excluded* spend
already recorded in the store, `spent_today` sums today's cost-bearing events — but it
returns `0.0` unless you tell it which runs to scan (see the
[edge case below](#spent_today-needs-run_ids)).

**Pick the model per step.** Routing sends low-stakes steps to cheap or local models and
hard steps to strong ones. A `RoutingPolicy` is an ordered list of `RoutingRule`s; the
first rule that *matches* an agent names the model field that agent should use. A rule
matches by exact `role`, by a coarse `CostTier` (`CHEAP` / `STANDARD` / `STRONG`), or
unconditionally. `CostTier` is advisory metadata an author pins on an agent (read back by
`agent_tier`); it labels the step, it does not itself pick a model. `route_decision`
resolves one agent through the policy and returns a `RoutingDecision` (the concrete model
id plus *why* it was chosen); `route_model` is the thin wrapper that returns just the id.
`routing_emission` turns a decision into a telemetry record so a dashboard can show why a
model was chosen.

**Skip the call entirely.** When the same definition-version and inputs run twice, the
second run can replay the first instead of paying again. `cache_key` hashes a request to
the key the replay layer uses — two requests share a key exactly when they would share a
recording. `CachingRuntime` wraps a [replay runtime](runtimes.md) and reports, per
request, whether it **hit** (free) or **missed** (paid), tallying both the dollars saved
and the dollars spent into a `CacheStats`.

---

## Ramps up

### The single resolution path (preview can't drift from the run)

A rule's chosen `model` is never a final id on its own — it is a *field* (`"local"`, a
configured alias, or a concrete id) handed to one shared resolver,
`crawfish.provider.resolve_model`. Both the runtime that actually runs an agent and
`estimate_cost`'s dry run call `route_decision` → `resolve_model`. There is no second
resolution path, so a routed step is previewed at exactly the model that will run. This
is the drift guarantee verified by CRA-186.

### `estimate_cost` heuristics and edge cases

The estimate is approximate by design and follows three rules:

- **One run per agent per item.** No retries, no delegation fan-out, no tool round-trips
  are modelled. It is a planning aid, not billing truth.
- **Unknown models are free.** `prices.get(model, 0.0)` — a model id missing from the
  table contributes `0.0` so a missing price never silently *inflates* the estimate. Pass
  a fuller `model_prices` table for sharper numbers. The built-in `DEFAULT_MODEL_PRICES`
  prices `mock` at `$0.00`, so test/replay pipelines preview at $0.
- **Negative item counts raise.** `items < 0` raises `ValueError`; `items == 0` is valid
  and yields a `$0.00` total.

Pass the project's `config` (a `ModelsConfig`) so the preview expands aliases and the
configured default exactly as the runtime will. Pass a `RoutingPolicy` and each agent's
model is resolved through routing first, so the preview prices the *routed* model.

### `Budget` vs the hard ceiling

`Budget` is the *soft* layer — it decides ok / warn / stopped. It does not kill a run;
the orchestrator's [`CostBudget`](context-and-budgets.md) is the *hard* ceiling that
raises `BudgetExceeded`. `Budget.as_cost_budget()` projects the `stop_usd` onto a matching
`CostBudget` so you configure one number and hand the hard half to the runtime. A
`stop_usd` of `None` means unbounded — every `check` returns `OK`. `__post_init__` defaults
`warn_usd` to 80% of `stop_usd` and raises `ValueError` if you set `warn_usd > stop_usd`.

### `spent_today` needs `run_ids`

`spent_today(store)` with no `run_ids` **returns `0.0` unconditionally** — it does not
scan the store. The Store seam is per-run (there is no cheap cross-run scan), so the
caller must pass the `run_ids` to total; absent them the function short-circuits at
`if run_ids is None: return 0.0` *before* reading any events. This is a real footgun: a
meter wired with the bare `spent_today(store)` reports $0 regardless of actual recorded
spend. Always pass `run_ids=[...]`. Within a scan, an event is kept when its `kind` is
cost-bearing (`model`, `run_finish`, `runtime.run`, `run.finish`) and its `ts` lands on
`today` (UTC); an event with an unparseable or zero timestamp is **counted**, never
silently dropped. (Surfaced as a doc finding — see `docs/BUILD-LOG.md`.)

### `CostTier` is advisory, read from string policies

A tier is declared on an agent as a string in its `policies` list (`"tier:cheap"` etc.)
and read back by `agent_tier`, which returns the `CostTier` or `None` if none is declared.
A `RoutingRule` with `tier=CostTier.CHEAP` matches only agents whose declared tier *is*
`CHEAP`; a rule with `tier=None` matches any tier. The tier never picks a model by itself —
it is a match condition the author uses to target a rule.

### `RoutingDecision.source` records why

`route_decision` always succeeds and records its reason in `source`: `"rule"` (a policy
rule fired), `"agent"` (no rule matched; the agent's own pinned `model` was used), or
`"default"` (no rule and the agent was unpinned, so the supplied `default` was used).
`routed` is `True` only in the `"rule"` case. Routing is purely additive — when no rule
matches, the agent's own `model` field is left intact; routing never strips an explicit
pin.

### Caching: keys, hits, and the within-session price

`cache_key` re-exports the replay layer's private `_key`, so a caller can compute hit/miss
without reaching into the runtime. The key hashes the definition id + version, role, model,
inputs, and session id. A `CachingRuntime.run` checks whether a cassette file already
exists for the key: on a **hit** it charges nothing and adds the recorded cost to
`saved_usd`; on a **miss** the inner replay runtime records and the model spends, and that
cost lands in `spent_usd`. A small in-process LRU (`track_capacity`, default 1024) remembers
each miss's cost so a repeated identical call within the same session is priced exactly,
even before the cassette is re-read. `CacheStats.hit_rate` is `hits / total`, or `0.0`
before anything runs. `CachingRuntime` is a [swappable `AgentRuntime`](runtimes.md) — it
wraps a `RecordReplayRuntime` and performs no model call itself.

---

## API reference

### `estimate_cost`

```python
def estimate_cost(
    definition: Definition,
    *,
    items: int = 1,
    model_prices: dict[str, float] | None = None,
    config: ModelsConfig | None = None,
    routing: RoutingPolicy | None = None,
) -> CostEstimate
```

Predict the dollar cost of running `definition` over `items` items: one run per agent per
item, priced by each agent's resolved model id from `model_prices` (defaults to
`DEFAULT_MODEL_PRICES`). Unknown model ids price as `0.0`. Raises `ValueError` if
`items < 0`. With `routing` set, each model is resolved through `route_decision` first.

`DEFAULT_MODEL_PRICES` (USD per run): `claude-opus-4-8` → `0.30`, `claude-sonnet-4-6` →
`0.06`, `claude-haiku-4-5` → `0.01`, `mock` → `0.00`.

### `CostEstimate`

`class CostEstimate(BaseModel)` — a dry-run cost preview. Frozen.

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `team_size` | `int` | — (required, `ge=0`) | Number of agents in the team. |
| `items` | `int` | — (required, `ge=0`) | Item count the total scales by. |
| `per_item_usd` | `float` | — (required, `ge=0.0`) | Predicted spend for one item across the whole team. |
| `per_model` | `dict[str, float]` | `{}` | Total broken down by resolved model id. |
| `total_usd` | `float` | — (required, `ge=0.0`) | `per_item_usd * items`. |

### `Budget`

`@dataclass class Budget` — a warn/stop spend policy.

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `stop_usd` | `float \| None` | `None` | Hard ceiling. `None` = unbounded (always `OK`). |
| `warn_usd` | `float \| None` | `None` | Soft line. Defaults to `0.8 * stop_usd` when unset and `stop_usd` is set. |

```python
def check(self, spent_usd: float) -> BudgetState
def as_cost_budget(self, *, spent_usd: float = 0.0) -> CostBudget
```

`__post_init__` raises `ValueError` if `warn_usd > stop_usd`.

### `BudgetState`

`class BudgetState(str, Enum)` — where spend sits relative to a `Budget`.

| Member | Value | Meaning |
| --- | --- | --- |
| `BudgetState.OK` | `"ok"` | Below the warn threshold. |
| `BudgetState.WARN` | `"warn"` | At/over warn, still below stop. |
| `BudgetState.STOPPED` | `"stopped"` | At/over the hard stop. |

### `CostMeter`

`@dataclass class CostMeter` — a live spend accumulator checked against a `Budget`.

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `budget` | `Budget` | `Budget()` | The policy `state()` checks against. |
| `total_usd` | `float` | `0.0` | Running spend. |

```python
def charge(self, amount_usd: float) -> BudgetState   # raises ValueError if amount_usd < 0
def state(self) -> BudgetState
@property
def remaining_usd(self) -> float | None              # headroom to stop, or None if unbounded
```

### `spent_today`

```python
def spent_today(
    store: Store,
    *,
    org_id: str = "local",
    run_ids: list[str] | None = None,
    today: date | None = None,
    now: datetime | None = None,
) -> float
```

Sum today's spend (UTC day) from cost-bearing events on the runs in `run_ids`.
**Returns `0.0` immediately when `run_ids is None`** — it does not scan the store, so the
caller must pass the runs to total. Cost-bearing kinds: `model`, `run_finish`,
`runtime.run`, `run.finish`. Events with an unparseable/zero `ts` are counted; events with
a usable `ts` on another day are excluded.

### `CostTier`

`class CostTier(str, Enum)` — coarse, advisory stakes/complexity label for a step.

| Member | Value | Meaning |
| --- | --- | --- |
| `CostTier.CHEAP` | `"cheap"` | Low-stakes/simple — route to a cheap or `local` model. |
| `CostTier.STANDARD` | `"standard"` | Unclassified middle. |
| `CostTier.STRONG` | `"strong"` | High-stakes/hard — route to the strong model. |

### `agent_tier`

```python
def agent_tier(agent: AgentSpec) -> CostTier | None
```

Read the `CostTier` an author declared in `agent.policies` (the first `"tier:<value>"`
entry that parses). Returns `None` when no valid tier is declared. Pure.

### `RoutingRule`

`class RoutingRule(BaseModel)` — one match→model rule. Frozen.

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `role` | `str \| None` | `None` | Exact role to match; `None` matches any role. |
| `tier` | `CostTier \| None` | `None` | Match agents whose *declared* tier equals this; `None` matches any tier. |
| `model` | `str \| list[str]` | — (required) | Target model **field** (resolved via `resolve_model`). A list is a failover order; preview uses its primary. |

`matches(agent) -> bool` — all set conditions must hold; unset conditions match anything.

### `RoutingPolicy`

`class RoutingPolicy(BaseModel)` — ordered rules, first match wins. Frozen.

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `rules` | `tuple[RoutingRule, ...]` | `()` | Evaluated in order; first match wins. |

`select_field(agent) -> str | list[str] | None` — the model field the first matching rule
names, or `None` when no rule matches (the agent's own `model` is then left intact).

### `RoutingDecision`

`class RoutingDecision(BaseModel)` — the deterministic outcome of routing one agent. Frozen.

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `role` | `str` | — (required) | The agent's role. |
| `resolved` | `str` | — (required) | Concrete model id, post shared-resolver. |
| `routed` | `bool` | — (required) | `True` when a policy rule fired. |
| `source` | `str` | — (required) | `"rule"`, `"agent"`, or `"default"` — why this id was chosen. |

### `route_decision`

```python
def route_decision(
    definition: Definition,
    role: str | None = None,
    *,
    policy: RoutingPolicy | None = None,
    default: str,
    config: ModelsConfig | None = None,
) -> RoutingDecision
```

Resolve one agent's model through `policy` then the shared `resolve_model`. The single
decision point both the runtime and `estimate_cost` route through. Deterministic; no I/O.

### `route_model`

```python
def route_model(
    definition: Definition,
    role: str | None = None,
    *,
    policy: RoutingPolicy | None = None,
    default: str,
    config: ModelsConfig | None = None,
) -> str
```

Thin wrapper over `route_decision` returning just the resolved model id.

### `routing_emission`

```python
def routing_emission(
    decision: RoutingDecision, *, run_id: str, org_id: str = "local"
) -> Emission
```

A typed `MODEL` `Emission` recording a routing decision. `cost_usd` is `0.0` (spend is
charged later when the model answers); the metadata (`model`, `routed_by`, `routed`) lives
under `attrs`. Not tainted — a routing choice derives from static config, never untrusted
input.

### `cache_key`

```python
def cache_key(request: RunRequest) -> str
```

The cassette key for `request` — a deterministic hash of definition id + version, role,
model, inputs, and session id. Two requests share a key exactly when they would share a
recording. Re-exports the replay layer's `_key`.

### `CacheStats`

`@dataclass class CacheStats` — running hit/miss + saved-spend accounting.

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `hits` | `int` | `0` | Requests served from the cassette (free). |
| `misses` | `int` | `0` | Requests not in the cassette (paid). |
| `saved_usd` | `float` | `0.0` | Spend each hit avoided. |
| `spent_usd` | `float` | `0.0` | Spend misses actually charged. |

`total` → `hits + misses`. `hit_rate` → `hits / total`, or `0.0` when nothing has run.

### `CachingRuntime`

`class CachingRuntime(AgentRuntime)` — a cost-aware wrapper over `RecordReplayRuntime`.
Constructor: `CachingRuntime(inner, *, cassette_dir=None, track_capacity=1024)`. Exposes a
live `stats: CacheStats`. Each `async run(request, ctx)` checks the cassette: a hit charges
nothing and adds to `saved_usd`; a miss records, spends, and adds to `spent_usd`. Performs
no model call itself.

---

## Example

Preview a two-agent run's cost, route by tier, and check cache-key determinism — all pure,
no runtime, no network.

```python
from crawfish.definition.types import Definition, TeamSpec, AgentSpec
from crawfish.cost import estimate_cost
from crawfish.routing import RoutingPolicy, RoutingRule, CostTier, route_model
from crawfish.cache import cache_key
from crawfish.runtime.base import RunRequest

# A two-agent team: a cheap "scout" and a "reviewer".
defn = Definition(team=TeamSpec(agents=[
    AgentSpec(role="scout",    model="claude-haiku-4-5", policies=["tier:cheap"]),
    AgentSpec(role="reviewer", model="claude-opus-4-8",  policies=["tier:strong"]),
]))

# 1) estimate_cost over 10 items with the default price table.
est = estimate_cost(defn, items=10)
print("per_item_usd:", round(est.per_item_usd, 2))
print("total_usd:   ", round(est.total_usd, 2))
print("per_model:   ", {k: round(v, 2) for k, v in sorted(est.per_model.items())})

# 2) route_model: a policy sends every CHEAP-tier step to the haiku model.
policy = RoutingPolicy(rules=(
    RoutingRule(tier=CostTier.CHEAP, model="claude-haiku-4-5"),
))
print("scout    ->", route_model(defn, "scout",    policy=policy, default="mock"))
print("reviewer ->", route_model(defn, "reviewer", policy=policy, default="mock"))

# 3) cache_key determinism: identical requests share a key; a changed input does not.
req_a = RunRequest(definition=defn, role="scout", inputs={"q": "hello"})
req_b = RunRequest(definition=defn, role="scout", inputs={"q": "hello"})
req_c = RunRequest(definition=defn, role="scout", inputs={"q": "world"})
print("a == b:", cache_key(req_a) == cache_key(req_b))
print("a == c:", cache_key(req_a) == cache_key(req_c))
print("keylen:", len(cache_key(req_a)))
```

??? success "▶ Output"

    ```text
    per_item_usd: 0.31
    total_usd:    3.1
    per_model:    {'claude-haiku-4-5': 0.1, 'claude-opus-4-8': 3.0}
    scout    -> claude-haiku-4-5
    reviewer -> claude-opus-4-8
    a == b: True
    a == c: False
    keylen: 16
    ```
