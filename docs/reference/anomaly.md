# Anomaly & auto-halt

The safety backstop that *acts* on a run's typed event stream: a small rule engine
watches the emissions a run produces and, when something looks runaway — cost spiking,
runs failing, a loop flooding events — escalates from a flagged finding all the way up
to killing the run. These live in `crawfish.anomaly`.

**Symbols on this page:** `Response` · `AnomalyRule` · `CostSpikeRule` ·
`FailureRateRule` · `StuckRunRule` · `EmissionFloodRule` · `BudgetApproachingRule` ·
`Firing` · `AnomalyEngine` · `read_and_guard`

---

## Core

Every running pipeline produces an **emission stream** — a sequence of typed events
(`crawfish.emission.Emission`): a run started, a model call cost this much, a run
finished with this status. The [observer](observer.md) layer *renders* that stream for
a dashboard. This module is the other half: it *reads* the same stream and reacts.

An **anomaly rule** is one deterministic check over those emissions. "Did model spend
in the last five minutes cross $2?" "Did more than half the recent runs fail?" Each rule
looks at the stream and either stays quiet or **fires**.

When a rule fires it carries a **response** — the tier of reaction it wants:

- **Flag** — record a visible warning. Nothing stops.
- **Alert** — record a critical warning. Nothing stops.
- **Halt** — the kill-switch. Stop the run.

A **firing** bundles the rule that tripped, the response tier, and the finding it
produced (a `crawfish.observe.ObserverEvent`, the same record type the dashboard shows).

The **anomaly engine** holds a set of rules. You hand it a stream of emissions; it runs
every rule once and gives back the list of firings. Its `guard` method goes further: it
saves the findings and, if any firing was a halt, **trips the run's kill-switch**.

That kill-switch is two levers on the run's context (`crawfish.core.context.RunContext`):

- its **cancel token** — a flag cooperative loops check between steps; cancelling it
  asks them to stop at the next checkpoint;
- its **cost budget** — a spend ceiling; forcing the ceiling below what's already been
  spent makes the next charge raise `BudgetExceeded`, blocking even a loop that ignores
  the cancel token.

**Why this is safe to trust.** A halt must never be triggerable by the very input the
agent is processing. So rules read *only* typed, numeric signals — a cost figure, a
failure count, an event count, a run's age — never free text that came from outside.
A value that derived from **fluid** (untrusted, per-item) data is marked *tainted* (carries
a flag warning it may be compromised); the firing records that taint for the dashboard, but
it never changes the decision, because
the decision was computed from numbers a compromised agent can't reach. And the rules
run in the **orchestrator** (the trusted parent process), never inside the sandboxed
child that runs the agent, so a hijacked agent can't switch them off.

---

## Ramps up

### The tiered response, and what "halt" actually does

`Response` is ordered `FLAG < ALERT < HALT`. The first two only differ in the severity
of the finding they emit (`WARN` vs `CRITICAL`); neither stops anything. `HALT` is the
runaway kill-switch. `AnomalyEngine.guard` calls `_halt(ctx)` once if *any* firing
halts, which:

1. calls `ctx.cancel_token.cancel()` — the cooperative lever; and
2. sets `ctx.cost_budget.limit_usd = spent_usd - 1e-9` — drops the ceiling a hair below
   current spend so the *next* `charge` raises `BudgetExceeded`.

Step 2 matters even when nothing has been spent yet (an `EmissionFloodRule` or
`StuckRunRule` can halt before any model call). With zero spend the ceiling goes
slightly negative on purpose, so the non-cooperative lever fires unconditionally on the
next `charge(...)`, including `charge(0.0)`. `_halt` is idempotent — calling it twice is
harmless.

### Determinism: rules never read a wall clock

Every rule is evaluated against a single `now` value. `AnomalyEngine.evaluate` resolves
it: if you don't pass `now`, it uses the **latest emission `ts`** in the stream (or `0.0`
for an empty stream). Time-windowed rules then compute their cutoff from that `now`
(via `parse_since`, e.g. `"-5m"` → `now - 300`), and age-based rules compute `now - ts`.
No rule consults the real clock in a way that affects its outcome, so a fixed synthetic
stream flags, alerts, and halts **identically every run** — which is what lets the
example below assert exact output.

### What each rule trips on

| Rule | Reads (from `Emission.attrs` / kind) | Breaches when |
| --- | --- | --- |
| `CostSpikeRule` | `cost_usd` on `MODEL` emissions in `window` | summed spend **≥** `threshold_usd` |
| `FailureRateRule` | `status` on `RUN_FINISH` emissions in `window` | failed fraction **>** `threshold` (no finishes → never) |
| `StuckRunRule` | `RUN_START` without matching `RUN_FINISH` | a run's age `now - start.ts` **>** `seconds` |
| `EmissionFloodRule` | emission count in `window` | count **≥** `max_count` |
| `BudgetApproachingRule` | `cost_usd` on all `MODEL` emissions | spend **≥** `budget_usd × fraction` |

Note the boundary conditions are deliberate and differ: cost/flood/budget are
inclusive (`≥`), failure rate and stuck-run are strict (`>`). `EmissionFloodRule` and
`BudgetApproachingRule` ignore taint and look across the whole stream (flood is windowed
by count, budget is cumulative); the others window by `ts`.

### Default responses differ per rule

Most rules default to `Response.FLAG`, but two don't: `EmissionFloodRule` defaults to
`HALT` (an event flood is a loop runaway — stop it), and `BudgetApproachingRule`
defaults to `ALERT` (an early warning *before* the hard `CostBudget` ceiling, while
there's still budget left to act on). Pass `response=` to override any of them.

### `guard` vs `evaluate`, and `read_and_guard`

`AnomalyEngine.evaluate` is **pure**: run the rules, return the firings, touch nothing.
`AnomalyEngine.guard` is the orchestrator entry point — it evaluates, emits each finding
onto an `ObserverSurface` (which also lands a typed `OBSERVER` emission back on the run
stream, so the breach is itself visible on the dashboard — see
[emission inspector & visualizer](emission-inspector-visualize.md)), and halts on any
halting firing. `read_and_guard` is the live-tail wiring the executor calls between
iterations: it reads the run's emissions from the store and `guard`s them.

`AnomalyEngine.enforce_budget(ctx, amount_usd)` is a separate convenience for spend
paths: it charges the budget and, on `BudgetExceeded`, also trips the cancel token so a
cooperative loop stops too.

---

## API reference

### `Response`

`class Response(str, Enum)` — the tier a breached rule escalates to. Ordered
`FLAG < ALERT < HALT`.

| Member | Value | Meaning |
| --- | --- | --- |
| `Response.FLAG` | `"flag"` | Emit a `WARN` finding; nothing stops. |
| `Response.ALERT` | `"alert"` | Emit a `CRITICAL` finding; nothing stops. |
| `Response.HALT` | `"halt"` | Trip the run's `CancelToken` and force `BudgetExceeded` — the kill-switch. |

Properties: `.severity` → `Severity.WARN` for `FLAG`, else `Severity.CRITICAL`;
`.halts` → `True` only for `HALT`.

### `AnomalyRule`

`class AnomalyRule(ABC)` — a deterministic check over the emission stream.

```python
def __init__(self, *, response: Response = Response.FLAG) -> None
```

Sets `self.response`. Subclasses set a class attribute `kind: str` (e.g. `"cost.spike"`)
and implement:

```python
@abstractmethod
def evaluate(
    self, emissions: Sequence[Emission], *, now: float, pipeline: str | None = None
) -> Firing | None: ...
```

Returns a `Firing` on breach, else `None`. The protected helper `_fire(...)` builds the
`ObserverEvent` (severity from `self.response.severity`, `observer="anomaly:{kind}"`) and
wraps it in a `Firing`, recording window taint via `_any_tainted`.

### `CostSpikeRule`

`class CostSpikeRule(AnomalyRule)` — `kind = "cost.spike"`.

```python
def __init__(
    self, *, threshold_usd: float, window: str = "-5m", response: Response = Response.FLAG
) -> None
```

| Param | Type | Default | Notes |
| --- | --- | --- | --- |
| `threshold_usd` | `float` | — (required) | Breach when summed `cost_usd` ≥ this. |
| `window` | `str` | `"-5m"` | Look-back window over `MODEL` emissions. |
| `response` | `Response` | `Response.FLAG` | Escalation tier. |

Breaches when summed `cost_usd` across `MODEL` emissions in `window` is **≥**
`threshold_usd`.

### `FailureRateRule`

`class FailureRateRule(AnomalyRule)` — `kind = "failure.rate"`.

```python
def __init__(
    self, *, threshold: float, window: str = "-1h", response: Response = Response.FLAG
) -> None
```

| Param | Type | Default | Notes |
| --- | --- | --- | --- |
| `threshold` | `float` | — (required) | Failed-fraction ceiling (e.g. `0.5`). |
| `window` | `str` | `"-1h"` | Look-back window over `RUN_FINISH` emissions. |
| `response` | `Response` | `Response.FLAG` | Escalation tier. |

Of the `RUN_FINISH` emissions in `window`, breaches when the fraction with
`attrs["status"] == "failed"` is **strictly greater than** `threshold`. No finishes in
window → never fires.

### `StuckRunRule`

`class StuckRunRule(AnomalyRule)` — `kind = "run.stuck"`.

```python
def __init__(self, *, seconds: float, response: Response = Response.FLAG) -> None
```

| Param | Type | Default | Notes |
| --- | --- | --- | --- |
| `seconds` | `float` | — (required) | Max allowed run age before breach. |
| `response` | `Response` | `Response.FLAG` | Escalation tier. |

Breaches when a run has a `RUN_START` but no `RUN_FINISH` and its age `now - start.ts` is
**strictly greater than** `seconds`. Reports the worst (oldest) stuck run.

### `EmissionFloodRule`

`class EmissionFloodRule(AnomalyRule)` — `kind = "emission.flood"`. The batch-level loop
cap, on count rather than cost.

```python
def __init__(
    self, *, max_count: int, window: str = "-1m", response: Response = Response.HALT
) -> None
```

| Param | Type | Default | Notes |
| --- | --- | --- | --- |
| `max_count` | `int` | — (required) | Emission-count cap. |
| `window` | `str` | `"-1m"` | Look-back window over **all** emissions. |
| `response` | `Response` | `Response.HALT` | Defaults to halt. |

Breaches when emission count in `window` is **≥** `max_count`.

### `BudgetApproachingRule`

`class BudgetApproachingRule(AnomalyRule)` — `kind = "budget.approaching"`. An early
warning before the hard `CostBudget` ceiling.

```python
def __init__(
    self, *, budget_usd: float, fraction: float = 0.8, response: Response = Response.ALERT
) -> None
```

| Param | Type | Default | Notes |
| --- | --- | --- | --- |
| `budget_usd` | `float` | — (required) | The budget being approached. Must be `> 0` or raises `ValueError`. |
| `fraction` | `float` | `0.8` | Fraction of `budget_usd` that trips the warning. |
| `response` | `Response` | `Response.ALERT` | Defaults to alert. |

Breaches when cumulative `cost_usd` across **all** `MODEL` emissions is **≥**
`budget_usd × fraction`.

### `Firing`

`@dataclass(frozen=True) class Firing` — a rule breach.

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `rule` | `AnomalyRule` | — (required) | The rule that tripped. |
| `response` | `Response` | — (required) | Its response tier. |
| `event` | `ObserverEvent` | — (required) | The finding emitted. |
| `tainted` | `bool` | `False` | Whether any emission in the judged window derived from fluid (untrusted) input. Recorded for the dashboard; never weakens the decision. |

Property: `.halts` → `self.response.halts`.

### `AnomalyEngine`

`class AnomalyEngine` — evaluate a set of rules over the stream and enforce halts.

```python
def __init__(self, rules: Sequence[AnomalyRule]) -> None
```

```python
def evaluate(
    self, emissions: Sequence[Emission], *,
    now: float | None = None, pipeline: str | None = None,
) -> list[Firing]
```

Runs every rule once; returns firings, **no side effects**. `now` defaults to the latest
emission `ts` (or `0.0` for an empty stream).

```python
def guard(
    self, ctx: RunContext, emissions: Sequence[Emission], *,
    now: float | None = None, pipeline: str | None = None,
    surface: ObserverSurface | None = None,
) -> list[Firing]
```

Evaluates, emits each finding onto `surface` (defaults to an `ObserverSurface` over
`ctx.store`), and on any halting firing trips `ctx.cancel_token` and forces
`ctx.cost_budget` over its ceiling. Returns all firings.

```python
@staticmethod
def enforce_budget(ctx: RunContext, amount_usd: float) -> None
```

Charges `amount_usd` against `ctx.cost_budget`; on `BudgetExceeded`, cancels the token
and re-raises.

### `read_and_guard`

```python
def read_and_guard(
    ctx: RunContext, engine: AnomalyEngine, *,
    run_id: str | None = None, pipeline: str | None = None,
    now: float | None = None, store: Store | None = None,
) -> list[Firing]
```

Reads a run's emissions from the store (`run_id` defaults to `ctx.run_id`, `store` to
`ctx.store`) via `crawfish.emission.read_emissions`, then `engine.guard`s them. The
live-tail point the executor calls between iterations. Deterministic given a fixed `now`.

---

## Example

A seeded synthetic stream: two of three runs failed, and $2.75 of model spend lands in
the window. `FailureRateRule` (alert) and `CostSpikeRule` (halt) both trip. Pure and
in-memory — `evaluate` touches no context, so no run is needed.

```python
from crawfish.emission import Emission, EmissionKind
from crawfish.anomaly import AnomalyEngine, FailureRateRule, CostSpikeRule, Response

def em(kind, *, ts, status=None, cost=None, run_id="r1"):
    attrs = {}
    if status is not None:
        attrs["status"] = status
    if cost is not None:
        attrs["cost_usd"] = cost
    return Emission(
        id=f"e-{ts}", kind=kind, run_id=run_id, pipeline="triage-bot",
        node_id="batch-0", ts=float(ts), attrs=attrs,
    )

emissions = [
    em(EmissionKind.RUN_FINISH, ts=10, status="failed", run_id="r1"),
    em(EmissionKind.RUN_FINISH, ts=20, status="failed", run_id="r2"),
    em(EmissionKind.RUN_FINISH, ts=30, status="ok",     run_id="r3"),
    em(EmissionKind.MODEL,      ts=40, cost=1.50),
    em(EmissionKind.MODEL,      ts=50, cost=1.25),
]

engine = AnomalyEngine([
    FailureRateRule(threshold=0.5, window="-1h", response=Response.ALERT),
    CostSpikeRule(threshold_usd=2.0, window="-5m", response=Response.HALT),
])

firings = engine.evaluate(emissions, now=50.0)
print(f"{len(firings)} firing(s):")
for f in firings:
    print(f"  [{f.rule.kind}] {f.response.value} halts={f.halts} :: {f.event.detail}")
print("any halts ->", any(f.halts for f in firings))
```

??? success "▶ Output"

    ```text
    2 firing(s):
      [failure.rate] alert halts=False :: 2/3 runs failed (67% > 50%)
      [cost.spike] halt halts=True :: $2.75 model spend in 5m (≥ $2.00)
    any halts -> True
    ```
