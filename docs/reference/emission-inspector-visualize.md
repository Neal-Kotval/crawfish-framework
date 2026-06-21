# Emission, inspector & visualize

The one typed event the whole system writes, and the two read-side tools that turn a
run's events back into something a human can see — a CLI report and a localhost
dashboard. Producers (runtime, tools, sinks, the secret broker, the sandbox jail,
observers, metrics) all write the same `Emission`; consumers read it back.

**Symbols on this page:** `Emission` · `EmissionKind` · `emit` · `read_emissions` ·
`REQUIRED_ATTRS` · `EMISSION_SCHEMA_VERSION` · `inspect_run` · `tail_events` ·
`format_report` · `RunReport` · `dashboard_state` · `serve_dashboard` ·
`emission_dashboard_state` · `collect_emissions` · `serve_emission_dashboard`

---

## Core

Crawfish keeps an **append-only ledger** of everything a run does — a list of events you
can add to but never edit or delete. Every event is one **`Emission`**: a single typed
record saying *what happened*. A model turn, a tool call, a write to an external system,
the start and finish of a run — each is one emission on the ledger.

Every emission has a **`kind`** drawn from a fixed list (`EmissionKind`): `run_start`,
`run_finish`, `model`, `tool`, `sink` (a consequential side effect — a write to an
external system), and so on. The kind tells a reader how to interpret the rest. The
free-form payload lives in **`attrs`**, a plain dictionary; each kind declares which keys
*must* be present (`REQUIRED_ATTRS`), so a reader can rely on a `model` emission carrying
a `cost_usd` and a `run_finish` carrying a `status`.

One field on an emission is load-bearing for security: **`tainted`**. A value is
*tainted* when it derives from **fluid** input — data that streamed in per item from
outside your control (a ticket body, a tool result), which is untrusted. The taint marker
rides across the emission boundary so the dashboard and any anomaly rules never treat
untrusted content as if it were trusted.

You write an emission with **`emit`** and read a run's emissions back with
**`read_emissions`**. On top of that read primitive sit two tools:

- **inspector** (`inspect_run`, `tail_events`, `format_report`, `RunReport`) — the
  CLI-level "what happened on this run": a summary report you can print.
- **visualize** (`dashboard_state`, `emission_dashboard_state`, `collect_emissions`,
  `serve_dashboard`, `serve_emission_dashboard`) — a zero-config dashboard that runs on
  your own machine (`127.0.0.1`, never reachable from the network) and re-renders the
  same data as a web page.

---

## Ramps up

### One signal, a closed taxonomy, a version

Telemetry used to be loose untyped dictionaries. `Emission` freezes the **contract**: a
single frozen model, a **closed** `EmissionKind` set (adding a kind is a deliberate
contract change), and an `EMISSION_SCHEMA_VERSION` integer (currently `1`) stamped on
every record so the ledger survives future kind/attr changes. The version is bumped
whenever the envelope or any kind's required attrs change; readers key off it to stay
forward- and backward-compatible. This taxonomy and the decision to carry the output
value inline in `attrs` are recorded in
[ADR 0013](../architecture/decisions/0013-emission-taxonomy-and-inline-output-value.md).

`REQUIRED_ATTRS` is the canonical per-kind schema — a frozen mapping (a read-only
`MappingProxyType`, so it can't drift accidentally) from each `EmissionKind` to the tuple
of `attrs` keys that kind must carry. `Emission.missing_attrs()` returns the required keys
absent from a given emission's `attrs`; `Emission.is_valid()` is true when none are
missing. These are pure contract checks — no I/O.

### Emissions are frozen; `ts` is caller-stamped

`Emission` sets `model_config = {"frozen": True}` — once created it cannot be mutated, the
same immutability discipline as frozen artifacts elsewhere in the framework. The timestamp
`ts` is **not** read from a wall clock by the model: it defaults to `0.0`, emitters stamp
it, and tests pass an explicit value for determinism. `emit` itself reads no clock.

### Reading is back-compatible by design

`read_emissions` lifts every ledger row through `Emission.from_event`, which handles both
new typed emissions (they round-trip exactly) and legacy loose dictionaries written before
the typed substrate existed. An unrecognized legacy dict still lifts into *some* emission
(a `metric` carrying the raw payload under `attrs["raw"]`) rather than raising — old runs
must remain inspectable. So a mixed ledger reads cleanly.

### `emit`'s flood cap

`emit` has an optional `max_per_run` volume cap guarding against an emission-flood
denial-of-service. If set and the run already holds at least that many events, the new
emission is dropped; the *first* time the cap is crossed, a single warning `observer`
emission (`attrs["kind"] == "emission.capped"`) is written in its place. The cap only
drops — it does not rotate or retain. If the store is wrapped in a redacting
`ScrubbingStore`, `emit` never bypasses it, so secrets are scrubbed on the write.

### inspector: derived, never live

`inspect_run` reads the typed stream via `read_emissions` and folds it into a `RunReport`:
status / total cost / latency from `run_finish`, accumulated cost from `model` emissions,
and an ordered transcript + tool-call list. `run_finish` cost is authoritative when
present; otherwise the per-`model` costs are summed. An unknown run (no events) yields a
report with `found=False` rather than a crash. It performs **no live model call** — it is a
pure read over append-only events. `tail_events` is the poll primitive behind `craw logs`:
pass the sequence index of the last event you saw and get only what is newer (`after_seq`
is a 0-based positional index; a negative value returns everything). `format_report`
renders a `RunReport` to a concise human-readable string.

### visualize: two dashboards, loopback only

There are two dashboards, each a single static HTML page plus one JSON endpoint, no build
step, polling to auto-refresh:

- **Topology dashboard** (`dashboard_state` / `serve_dashboard`, port `7878`) — deployed
  pipelines, recent runs, today's spend, observer feed. `dashboard_state` builds its JSON
  purely from the scrubbed Store surface.
- **Emission dashboard** (`emission_dashboard_state` / `collect_emissions` /
  `serve_emission_dashboard`, port `7879`) — a *generic* projection over the typed
  emission stream. `emission_dashboard_state` is **pure** (no clock, no socket, no Store):
  give it emissions and it buckets them per kind (count + the union of every `attrs` key
  seen), aggregates every *numeric* `attrs` value into a metric series keyed
  `"<kind>.<attr>"` (so `model.cost_usd` becomes total spend with no bespoke code, and a
  brand-new numeric attr does too), and lists every emission as an event row. Taint
  propagates: any kind/metric/run touched by a tainted emission is flagged. A few attrs
  (`node_id`, `session_id`, `output_id`, `batch_id`, `cap`) are never treated as metric
  series even when numeric-looking. `collect_emissions` is the Store-backed feeder: it
  enumerates runs via the run-info surface and lifts each run's ledger through
  `read_emissions`, filtered to a `since` window. (It only sees runs the run-info surface
  knows about — emissions written to a bare run id with no registered pipeline won't be
  enumerated; feed `emission_dashboard_state` from `read_emissions` directly for those.)

Both `serve_*` functions bind `127.0.0.1` only — never `0.0.0.0` — and reject non-loopback
`Host` headers (a DNS-rebinding defense). They read only the already-scrubbed ledger, so no
secret value is ever rendered, and the emission dashboard has no write path or egress.

> `serve_dashboard` and `serve_emission_dashboard` **start an HTTP server**; the caller
> runs `serve_forever()`. Do not call them in a non-interactive script — the example below
> uses the pure state builders instead.

---

## API reference

### `EMISSION_SCHEMA_VERSION`

`EMISSION_SCHEMA_VERSION: int = 1` — the ledger schema version stamped on every
`Emission`. Bumped when the envelope or any kind's required attrs change.

### `EmissionKind`

`class EmissionKind(str, Enum)` — the **closed** taxonomy of signals (10 members).

| Member | Value | Meaning |
| --- | --- | --- |
| `RUN_START` | `"run_start"` | A pipeline/agent run began. |
| `RUN_FINISH` | `"run_finish"` | A run completed (terminal). |
| `MODEL` | `"model"` | One model turn (cost / tokens / model id). |
| `TOOL` | `"tool"` | A tool/MCP call (result is untrusted → tainted). |
| `SINK` | `"sink"` | A consequential side effect was attempted/committed. |
| `COMPACTION` | `"compaction"` | Context was compacted/summarized. |
| `OBSERVER` | `"observer"` | An observer event crossed into the stream. |
| `METRIC` | `"metric"` | A measured metric/rubric value. |
| `SECRET_LEASE` | `"secret_lease"` | The broker leased a secret to a node. |
| `JAIL_VIOLATION` | `"jail_violation"` | The sandbox blocked an escape attempt. |

### `REQUIRED_ATTRS`

`REQUIRED_ATTRS: Mapping[EmissionKind, tuple[str, ...]]` — a frozen (`MappingProxyType`)
map of the `attrs` keys each kind must carry.

| Kind | Required `attrs` keys |
| --- | --- |
| `RUN_START` | `("runtime",)` |
| `RUN_FINISH` | `("status",)` |
| `MODEL` | `("model", "cost_usd")` |
| `TOOL` | `("tool",)` |
| `SINK` | `("target", "committed")` |
| `COMPACTION` | `("strategy",)` |
| `OBSERVER` | `("kind", "severity")` |
| `METRIC` | `("metric", "value")` |
| `SECRET_LEASE` | `("ref", "node_id")` |
| `JAIL_VIOLATION` | `("attempt", "severity")` |

### `Emission`

`class Emission(BaseModel)` — one typed signal on the ledger. `model_config =
{"frozen": True}`.

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `id` | `str` | `new_id()` | Opaque UUID4 identifier. |
| `schema_version` | `int` | `EMISSION_SCHEMA_VERSION` (`1`) | Ledger schema version. |
| `kind` | `EmissionKind` | — (required) | What kind of signal this is. |
| `run_id` | `str` | — (required) | The run this belongs to. |
| `org_id` | `str` | `"local"` | Tenancy key. |
| `pipeline` | `str \| None` | `None` | Owning pipeline, when applicable. |
| `node_id` | `str \| None` | `None` | Emitting agent/node, when applicable. |
| `ts` | `float` | `0.0` | Epoch seconds; emitters stamp it, tests pass it for determinism. |
| `attrs` | `dict[str, JSONValue]` | `{}` | Kind-specific payload (see `REQUIRED_ATTRS`). |
| `tainted` | `bool` | `False` | `True` when any `attrs` value derives from fluid (untrusted) input. |

Methods: `missing_attrs() -> tuple[str, ...]` (required keys absent from `attrs`);
`is_valid() -> bool` (no missing keys); `to_event() -> dict[str, JSONValue]`
(JSON-safe ledger dict); `from_event(event) -> Emission` (classmethod; rehydrates typed or
legacy dicts).

### `emit`

```python
def emit(
    store: Store,
    e: Emission,
    *,
    org_id: str = "local",
    max_per_run: int | None = None,
) -> None
```

Write a typed `Emission` to the ledger via `Store.append_event`. With `max_per_run` set,
drops the emission once the run holds at least that many events, writing one
`emission.capped` warning `observer` the first time the cap is crossed. Reads no wall
clock; respects a wrapping `ScrubbingStore`.

### `read_emissions`

```python
def read_emissions(store: Store, run_id: str, *, org_id: str = "local") -> list[Emission]
```

Read a run's ledger and lift every event into a typed `Emission` (typed rows round-trip;
legacy loose dicts lift via the back-compat shim). Pure read — no clock.

### `RunReport`

`class RunReport(BaseModel)` — a summary of one run, derived from the event ledger.

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `run_id` | `str` | — (required) | The run summarized. |
| `found` | `bool` | `False` | `False` for an unknown run (no events). |
| `status` | `str` | `"unknown"` | From the `run_finish` emission. |
| `cost_usd` | `float` | `0.0` | `run_finish` cost if present, else summed `model` costs. |
| `latency_ms` | `float \| None` | `None` | From `run_finish`. |
| `tool_calls` | `list[ToolCallRecord]` | `[]` | Tool invocations recovered from the stream. |
| `transcript` | `list[TranscriptEntry]` | `[]` | Ordered transcript lines. |
| `event_count` | `int` | `0` | Number of emissions read. |

### `inspect_run`

```python
def inspect_run(store: Store, run_id: str, *, org_id: str = "local") -> RunReport
```

Summarize a run from the ledger into a `RunReport`. Reads the typed stream via
`read_emissions`; performs no live model call. An unknown run yields `found=False`.

### `tail_events`

```python
def tail_events(
    store: Store, run_id: str, *, after_seq: int = 0, org_id: str = "local"
) -> list[dict[str, JSONValue]]
```

Return raw ledger events after `after_seq` (a 0-based positional index). `after_seq=0`
skips the first event; a negative value returns everything. The poll primitive behind
`craw logs`.

### `format_report`

```python
def format_report(report: RunReport) -> str
```

Render a `RunReport` to a concise human-readable string (status, cost, latency, events,
tool calls, transcript). An unfound report renders a one-line "not found".

### `dashboard_state`

```python
def dashboard_state(
    store: Store, *, org_id: str = "local",
    now: datetime | None = None, event_window: str = "-24h",
) -> dict[str, JSONValue]
```

Build the topology dashboard's JSON — `pipelines`, `recent_runs`, `cost_today_usd`,
`observer_events`, `generated_at` — purely from the scrubbed Store surface.

### `serve_dashboard`

```python
def serve_dashboard(
    store: Store, *, org_id: str = "local", port: int = 7878
) -> ThreadingHTTPServer
```

Create a loopback-bound (`127.0.0.1`) topology dashboard server. **Starts a server** — the
caller runs `serve_forever()`.

### `emission_dashboard_state`

```python
def emission_dashboard_state(
    emissions: Iterable[Emission], *, generated_at: float = 0.0
) -> dict[str, JSONValue]
```

Build the emission dashboard's JSON purely from a typed emission stream — **pure** (no
clock, no socket, no Store). Returns `generated_at`, `emission_count`, `tainted_count`,
`total_cost_usd` (the `model.cost_usd` series sum), `kinds`, `metrics`, `runs`, and the
newest 200 `events`. Generic: numeric `attrs` become aggregated metric series, taint is
propagated, a new attr appears with no dashboard-specific code.

### `collect_emissions`

```python
def collect_emissions(
    store: Store, *, org_id: str = "local",
    since: str | float | int | None = None, now: float | None = None,
) -> list[Emission]
```

Gather typed emissions across all runs known to the run-info surface, filtered to the
`since` window. Pure read; the only clock use is resolving a relative `since`.

### `serve_emission_dashboard`

```python
def serve_emission_dashboard(
    store: Store, *, org_id: str = "local",
    port: int = 7879, since: str | float | int | None = None,
) -> ThreadingHTTPServer
```

Create a loopback-bound (`127.0.0.1`) emission dashboard server. **Starts a server** — the
caller runs `serve_forever()`. No write path, no egress.

---

## Example

Emit three typed emissions to an in-memory store, read them back, and project them through
both the inspector and the pure emission-dashboard state builder. `ts` is stamped by hand
so nothing reads a clock. No server is started.

```python
from crawfish.store.sqlite import SqliteStore
from crawfish.emission import (
    Emission, EmissionKind, emit, read_emissions,
    REQUIRED_ATTRS, EMISSION_SCHEMA_VERSION,
)
from crawfish.inspector import inspect_run
from crawfish.visualize import emission_dashboard_state

store = SqliteStore(":memory:")
run = "run-1"

emit(store, Emission(kind=EmissionKind.RUN_START, run_id=run,
                     attrs={"runtime": "mock"}, ts=1.0))
emit(store, Emission(kind=EmissionKind.MODEL, run_id=run, node_id="agent",
                     attrs={"model": "claude", "cost_usd": 0.25}, ts=2.0))
emit(store, Emission(kind=EmissionKind.RUN_FINISH, run_id=run,
                     attrs={"status": "done", "cost_usd": 0.25, "latency_ms": 12.0}, ts=3.0))

print("schema_version:", EMISSION_SCHEMA_VERSION)
print("emission kinds:", len(EmissionKind))
print("MODEL required attrs:", REQUIRED_ATTRS[EmissionKind.MODEL])

# Read the typed stream back; every emission satisfies its kind's schema.
ems = read_emissions(store, run)
print("read back:", len(ems), "valid:", all(e.is_valid() for e in ems))

# inspector folds the stream into a RunReport.
rep = inspect_run(store, run)
print("status:", rep.status, "cost:", rep.cost_usd, "events:", rep.event_count)

# The pure dashboard projection — feed it the emissions directly.
state = emission_dashboard_state(ems, generated_at=0.0)
print("emission_count:", state["emission_count"])
print("total_cost_usd:", state["total_cost_usd"])
print("kinds seen:", len(state["kinds"]))
```

??? success "▶ Output"

    ```text
    schema_version: 1
    emission kinds: 10
    MODEL required attrs: ('model', 'cost_usd')
    read back: 3 valid: True
    status: done cost: 0.25 events: 3
    emission_count: 3
    total_cost_usd: 0.25
    kinds seen: 3
    ```
