# Observers — watch a running pipeline

An **Observer** polls a running pipeline's event stream on a cron interval and emits an
`ObserverEvent` when something looks wrong. It catches a failure-rate spike, a cost
spike, or a stuck or slow run. Add an LLM judge and it also flags low-quality results,
described in plain language. Observers write to the same scrubbed run-info surface that
the [dashboard](visualize.md) and [`craw manage`](manage.md) read from, so an alert
shows up in every view at once.

Observers live in `crawfish.observe`, alongside the run-info surface below.

## The run-info surface

Nodes, observers, and the deploy supervisor record what happened through
`ObserverSurface`, a Store-backed handle:

```python
from crawfish.observe import ObserverSurface, ObserverEvent, RunInfo, Severity

surface = ObserverSurface(store, org_id="local")

# emit an event (nodes call ctx.emit(...) — same payload)
surface.emit(ObserverEvent(
    pipeline="triage-bot",
    kind="cost.spike",
    detail="run cost $0.31 > 2x median",
    severity=Severity.warn,
    observer="cost-watch",
    run_id="01HZ…",
))

# record per-run rollup
surface.put_run_info(RunInfo(
    pipeline="triage-bot",
    run_id="01HZ…",
    status="ok",
    backend="command",
    version="0.3.1",
    cost_usd=0.31,
    items=3,
))

# read back
events = surface.events("triage-bot", since="-1h", kind="cost.spike")
runs   = surface.run_info("triage-bot", since="-1d")
```

| Type            | Fields |
| --------------- | ------ |
| `ObserverEvent` | `pipeline, kind, detail, severity, observer, run_id, ts, data` |
| `RunInfo`       | `pipeline, run_id, status, backend, version, cost_usd, items, started_at, finished_at` |
| `Severity`      | `info` · `warn` · `critical` |

`since=` takes a relative window (`"-1h"`, `"-30m"`, `"-15s"`, `"-2d"`) or an epoch
timestamp. Inside a node or observer, you emit through the run context:

```python
ctx.emit(ObserverEvent(pipeline="triage-bot", kind="item.dropped",
                       detail="missing ticket_body", severity=Severity.info))
```

## Defining an observer

An `Observer` polls a pipeline's event stream on a cron interval and applies rules:

- **Rule-based** (pure and free): failure rate over a window, cost spike against the
  median, and latency or stuck-run detection.
- **LLM / Definition-backed judge** (optional): a Definition reads recent run data and
  flags low-quality runs in plain language.

```python
from crawfish.observe import Observer, Severity
from crawfish import Definition

watch = Observer(
    pipeline="triage-bot",
    interval="*/5 * * * *",                 # poll every 5 minutes
    rules=[
        Observer.failure_rate(threshold=0.2, window="-15m", severity=Severity.warn),
        Observer.cost_spike(factor=2.0, severity=Severity.warn),
        Observer.stuck(after="-10m", severity=Severity.critical),
    ],
    judge=Definition.from_package("observers/quality"),   # optional NL judge
)
```

When a rule trips, the observer emits an `ObserverEvent` onto the same surface. From
there `craw manage logs`, the dashboard, and any downstream alert sink pick it up.

## Worked example — guard the deployed triage bot

Deploy the pipeline, then attach an observer that warns on cost spikes and runs a
quality judge:

```bash
craw deploy demo/triage-bot --schedule "0 8 * * *"
```

```python
from crawfish.observe import Observer, ObserverSurface, Severity
from crawfish import Definition, SqliteStore

surface = ObserverSurface(SqliteStore(), org_id="local")

watch = Observer(
    pipeline="triage-bot",
    interval="*/5 * * * *",
    rules=[
        Observer.cost_spike(factor=2.0, severity=Severity.warn),
        Observer.failure_rate(threshold=0.25, window="-30m", severity=Severity.critical),
    ],
    judge=Definition.from_package("observers/quality"),
)
await watch.run(surface)                    # polls; emits events as rules trip
```

Watch the events arrive:

```bash
craw manage logs crawfish/triage-bot
# 08:00:05  observer cost.spike     severity=warn      detail="run cost $0.31 > 2x median"
# 08:05:00  observer quality.flag   severity=warn      detail="summary omits the root cause"
```

Or query them directly:

```python
events = surface.events("triage-bot", since="-1h", kind="quality.flag")
```

## Security

An LLM/Definition-backed observer runs under the **same boundary as any Definition**:

- **Prompt-injection boundary.** Run data the judge reads is **fluid** data. It reaches
  the model inside a labelled data block, never as instructions. A malicious ticket body
  that surfaces in a run cannot turn the observer into an attacker's agent.
- **Cost-capped and metered.** The judge spends under the same `CostBudget` and
  `CostMeter` as a normal run. Its spend is metered and shows up in `$ today`. There is
  no unbounded background LLM cost.
- **Scrubbed surface, tenancy.** Events and run-info are scrubbed before the Store write.
  The surface wraps `ScrubbingStore` (reused, not reinvented), and every row carries
  `org_id`. No secret value reaches an event, the dashboard, or a log.

See the [operations overview](operations.md) and [SECURITY.md](../architecture/SECURITY.md).
