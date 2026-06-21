# Context & budgets

The handle a node holds while it runs: who it is, where it persists state, and the
two levers the orchestrator pulls to stop runaway work — a cost ceiling and a
cooperative cancel signal. These live in `crawfish.core.context` and are threaded
through every step of a run.

**Symbols on this page:** `RunContext` · `CostBudget` · `CancelToken` · `BudgetExceeded` · `Cancelled`

---

## Core

When a node executes, it receives a **run context** — a single object carrying
everything that step needs to know about *this* run: a `run_id` identifying the run,
the `org_id` it belongs to (the tenancy key — which customer/workspace owns the data),
a `store` for reading and writing persistent state, and the two safety levers below.

A **cost budget** is a spending ceiling measured in dollars. Each unit of work
(typically a model call) **charges** its cost against the budget. While the running
total stays under the cap, charges succeed silently. The first charge that pushes the
total *over* the cap raises `BudgetExceeded` — the orchestrator's hard kill on a run
that is burning money faster than allowed. A budget with no cap (the local-dev default)
never raises.

A **cancel token** is a cooperative stop signal. Cancellation here is *cooperative*:
nothing force-kills the node mid-instruction. Instead, long-running loops periodically
**check in** by calling `raise_if_cancelled`. While the token is clear, that check does
nothing. Once something **trips** the token (by calling `cancel`), the next check-in
raises `Cancelled` and the loop unwinds. The token can also be polled without raising,
via its `cancelled` property.

`BudgetExceeded` and `Cancelled` are both the exceptions these two levers raise — the
signals that travel up the stack when a run hits its ceiling or is told to stop.

---

## Ramps up

### One context, threaded everywhere

`RunContext` is a plain dataclass, not a Pydantic model — it bundles live handles
(a `Store`, a `threading.Event`-backed token), not serialisable data. Every node in a
pipeline receives the *same* context for a run, so the cost budget and cancel token are
shared state: a charge in one node counts against the same ceiling every other node
shares, and one `cancel()` stops them all.

The `Store` type is imported only under `TYPE_CHECKING` — `core` is the substrate the
rest of the framework sits on, so it depends on the store *protocol*, never a concrete
backend. This keeps the module dependency-light and free of import cycles.

### Charging is additive, and the check fires after

`CostBudget.charge` adds to `spent_usd` *first*, then tests the total against the cap.
So the spend total reflects the charge even when that charge is the one that trips the
limit — after a `BudgetExceeded`, `spent_usd` already includes the over-the-line amount.
The comparison is strict (`>`): spending *exactly* the cap is allowed; only spending
*past* it raises.

A cap of `None` means unbounded. `charge` then never raises (the `limit_usd is not None`
guard short-circuits), and `remaining_usd` returns `None` rather than a number — there is
no remaining budget to report when there is no limit.

### Cancellation is cooperative, not pre-emptive

`CancelToken` wraps a `threading.Event`. `cancel()` sets it; `cancelled` reads it;
`raise_if_cancelled()` raises `Cancelled` only if it is set. Because nothing interrupts
the node on its behalf, a node that never calls `raise_if_cancelled` will run to
completion regardless of cancellation — the contract is that long loops opt in by
checking in. The `threading.Event` backing means a token tripped from another thread
(an orchestrator watchdog) is observed safely by the node thread.

### `emit` routes through the store

`RunContext.emit` appends an observer event for the run-info surface, but it does so
*through this run's `store`*. That indirection is load-bearing: a `ScrubbingStore`
wrapper around the store redacts secrets before the write, so emitted events cannot leak
credentials — the secret/prompt-injection boundary. The import of `ObserverSurface` is
deliberately function-local to avoid a `core ↔ observe` import cycle.

---

## API reference

### `BudgetExceeded`

`class BudgetExceeded(RuntimeError)` — raised by `CostBudget.charge` when a charge would
push cumulative spend past the cap.

### `Cancelled`

`class Cancelled(RuntimeError)` — raised by `CancelToken.raise_if_cancelled` when the
token has been cancelled.

### `CostBudget`

`@dataclass class CostBudget` — a dollar ceiling the orchestrator can hard-kill on.

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `limit_usd` | `float \| None` | `None` | The cap. `None` means unbounded (local-dev default). |
| `spent_usd` | `float` | `0.0` | Cumulative amount charged so far. |

| Member | Signature | Behaviour |
| --- | --- | --- |
| `charge` | `charge(self, amount_usd: float) -> None` | Adds `amount_usd` to `spent_usd`, then raises `BudgetExceeded` if a cap is set and `spent_usd > limit_usd`. The spend is recorded even when the charge trips the limit. |
| `remaining_usd` | `remaining_usd: float \| None` (property) | `max(0.0, limit_usd - spent_usd)` when a cap is set; `None` when unbounded. Never negative. |

### `CancelToken`

`@dataclass class CancelToken` — cooperative cancellation backed by a `threading.Event`.

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `_event` | `threading.Event` | new `Event` per token | Internal signal. Not part of the public surface; use the methods below. |

| Member | Signature | Behaviour |
| --- | --- | --- |
| `cancel` | `cancel(self) -> None` | Trips the token (sets the event). Idempotent. |
| `cancelled` | `cancelled: bool` (property) | `True` once `cancel()` has been called; does not raise. |
| `raise_if_cancelled` | `raise_if_cancelled(self) -> None` | Raises `Cancelled` if the token is tripped; no-op otherwise. The cooperative check-in long loops call. |

### `RunContext`

`@dataclass class RunContext` — per-run execution context handed to every node.

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `store` | `Store` | — (required) | Persistence handle; a `Store` protocol implementation. |
| `run_id` | `str` | `new_id()` | Opaque identifier for this run (UUID4 string). |
| `batch_id` | `str \| None` | `None` | Identifier of the enclosing batch, if any. |
| `org_id` | `str` | `"local"` | Tenancy key — which org/workspace owns the run. |
| `cost_budget` | `CostBudget` | new `CostBudget()` | The run's shared spend ceiling (unbounded by default). |
| `cancel_token` | `CancelToken` | new `CancelToken()` | The run's shared cancel signal. |

| Member | Signature | Behaviour |
| --- | --- | --- |
| `emit` | `emit(self, event: ObserverEvent) -> None` | Appends an observer event, routed through `store` so a `ScrubbingStore` wrapper can redact secrets before the write. |

---

## Example

A budget that charges under its cap, then trips it; an unbounded budget; and a cancel
token polled and tripped — all pure, no runtime needed.

```python
from crawfish.core.context import CostBudget, BudgetExceeded, CancelToken, Cancelled

# A budget with a $1.00 cap.
budget = CostBudget(limit_usd=1.00)
budget.charge(0.60)                       # under the cap — ok
print(f"spent ${budget.spent_usd:.2f}, remaining ${budget.remaining_usd:.2f}")

try:
    budget.charge(0.75)                    # would push spend to $1.35 > $1.00
except BudgetExceeded as exc:
    print(f"BudgetExceeded: {exc}")

# An unbounded budget never raises and reports no remaining.
free = CostBudget()
free.charge(1000.0)
print(f"unbounded remaining: {free.remaining_usd}")

# A cancel token: clear, then tripped.
token = CancelToken()
token.raise_if_cancelled()                 # no-op while clear
print(f"cancelled before: {token.cancelled}")
token.cancel()
print(f"cancelled after: {token.cancelled}")
try:
    token.raise_if_cancelled()
except Cancelled as exc:
    print(f"Cancelled: {exc}")
```

??? success "▶ Output"

    ```text
    spent $0.60, remaining $0.40
    BudgetExceeded: cost budget exceeded: spent $1.3500 > $1.0000
    unbounded remaining: None
    cancelled before: False
    cancelled after: True
    Cancelled: run was cancelled
    ```
