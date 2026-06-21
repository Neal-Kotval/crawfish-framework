# Operate — deploy, manage & triggers

Everything that turns a finished pipeline into something that *runs by itself*: launching
it as a long-lived background process, listing and restarting what's running, deciding
*when* it fires, and packaging it as a container image. These live in `crawfish.deploy`,
`crawfish.manage`, `crawfish.triggers`, and `crawfish.build`.

**Symbols on this page:** `DeployEntry` · `DeployRegistry` · `DeployStatus` · `Supervisor` ·
`deploy` · `stop` · `PipelineStatus` · `manage_list` · `format_table` · `restart_target` ·
`Cron` · `CronSchedule` · `Trigger` · `CronTrigger` · `WebhookTrigger` · `verify_webhook` ·
`generate_containerfile` · `plan_build` · `write_containerfile` · `BuildPlan`

---

## Core

A pipeline you run by hand stops when you close the terminal. **Deploying** it means
launching it as a **daemon** — a long-lived background process that survives the shell
closing, fires the pipeline on a schedule (or continuously), restarts a failed cycle, and
picks up unfinished work again after a crash. In Crawfish that daemon is the
**`Supervisor`**, and `deploy` is the function that spawns it detached and remembers it.

To "remember it" Crawfish writes a row into the **deploy registry** — a small table, kept
in the project's [`Store`](persistence.md), recording each deployed pipeline's name, process id
(**PID**, the operating system's number for a running process), schedule, and status. One
row is a **`DeployEntry`**; `DeployRegistry` reads and writes them; `DeployStatus` is the
running / stopped / dead label on each. `stop` signals the process and flips its status.

**`craw manage`** is the operator's window onto all of that. `manage_list` builds the
view — one **`PipelineStatus`** per deployed pipeline, joining the registry row with how
its runs actually went (uptime, last run, cost today, next fire time). `format_table`
prints those rows as a plain text table. `restart_target` stops a pipeline and
re-deploys it from its recorded directory and schedule.

A **trigger** answers *when does this fire?* Two kinds:

- A **cron schedule** — the classic Unix five-field time spec (`m h dom mon dow`,
  e.g. `0 8 * * *` = "08:00 every day"). `CronSchedule` evaluates one; `Cron` is just a
  shorter name for the same class; `CronTrigger` is the object a project declares to say
  "fire on this cron string". `Trigger` is the abstract base both trigger kinds share.
- A **webhook** — instead of polling a clock, an external system sends an HTTP request
  that fires a run. `WebhookTrigger` describes the endpoint. Because anyone can send an
  HTTP request, the sender proves it is genuine by attaching a **signature** computed from
  a shared secret; `verify_webhook` recomputes that signature and checks it matches.

Finally, **`craw build`** packages a project as a container image — a self-contained,
reproducible bundle of the project plus its pinned dependencies. You never hand-write the
build recipe (the **`Containerfile`**): `generate_containerfile` derives it
deterministically, `write_containerfile` saves it, and `plan_build` returns a
**`BuildPlan`** summarising what the image will be without writing anything.

---

## Ramps up

### Why a detached daemon, not tmux

`deploy` spawns the supervisor as a true detached child via
`subprocess.Popen(..., start_new_session=True)` — its own session leader, so it outlives
the shell. An earlier option was to run it inside a `tmux` session; that was rejected
because it adds a runtime dependency, hides the process from the registry, and makes the
control surface (`craw manage`) depend on parsing `tmux` output. The daemon is visible,
dependency-free, and controllable through the Store-backed registry. See
[ADR 0009](../architecture/decisions/0009-deploy-detached-daemon-over-tmux.md) (daemon vs tmux).

### The supervisor never carries a secret

The detached child's command line is `python -m crawfish.cli _supervise <name> --dir <dir>
[--schedule <s>]` — the pipeline name, its directory, and an optional schedule string.
**No secret value ever appears** in argv, in the session name (`crawfish/<name>`), or in
an env dump. Secrets are resolved by reference at run time, exactly as in a foreground run,
and `supervise_main` wraps the project's Store in a `ScrubbingStore` so nothing a cycle
writes can leak a credential. A failed cycle's exception text is additionally passed
through `redact` before it becomes an observer event — defence in depth.

### Logic is split from the spawn, so it's testable

`Supervisor`'s scheduling and one-cycle logic is deliberately separated from the act of
launching a daemon. Tests drive `run_cycle(now)` and `due(now)` directly, and `serve`
takes injectable `now_fn` / `sleep_fn` / `stop_flag` / `max_cycles` seams so the always-on
loop runs in zero real time. Likewise `deploy` and `stop` take an injectable `spawn` /
`kill` callable. Nothing on this page requires an actual daemon to exercise.

### Liveness reconciliation: reality over stale state

A process can crash without cleaning up its registry row, leaving a stale `running`.
`DeployRegistry.reconcile_liveness` checks each `RUNNING` row's PID with `os.kill(pid, 0)`
(a probe that delivers no signal) and marks the vanished ones `DEAD`. `manage_list` calls
this on every read, so the operator sees what is actually alive, not what was last
recorded. Redeploying over a still-live PID of the same name emits a `deploy.replaced`
warning rather than silently orphaning the old process.

### Cron semantics that match real cron

`CronSchedule` is minute-resolution and supports `*`, `*/n` steps, `a,b` lists, `a-b`
ranges, and exact values. Day-of-week is `0-6` with **Sunday = 0**. Following standard
cron, when *both* day-of-month and day-of-week are restricted, a tick matches if **either**
matches; otherwise both must. `next_after` searches forward minute by minute for up to a
year and raises `ValueError` if nothing matches. (`parse_schedule` also accepts `@every 30s`
style strings, returning an `IntervalSchedule` for sub-minute cadence — documented with the
trigger internals; the `Cron`/cron path is the focus here.)

### Webhook verification is constant-time

`verify_webhook` computes `HMAC-SHA256(secret, payload)` as lowercase hex and compares it
to the supplied signature with `hmac.compare_digest`. The constant-time compare matters:
a naive `==` returns early on the first differing byte, leaking through timing how much of
a forged signature was correct. The `secret` itself is never stored inline —
`WebhookTrigger.secret_ref` holds the *name* of an environment variable, and the caller
resolves the value from there before calling `verify_webhook`.

### Builds are deterministic by construction

Both `generate_containerfile` and `plan_build` are pure functions of the manifest plus two
keyword switches (`python_version`, `lock_present`) — identical input yields byte-identical
output, so an image is reproducible. The base image is `python:<version>-slim`; the image
tag is `<name>:<version>` from the manifest; the entrypoint is always `craw run`, so the
container *is* the runnable automation. When `lock_present` is true the recipe copies and
installs `crawfish.lock` first for pinned dependencies.

---

## API reference

### `DeployStatus`

`class DeployStatus(str, Enum)` — the lifecycle label on a deployed pipeline.

| Member | Value | Meaning |
| --- | --- | --- |
| `DeployStatus.RUNNING` | `"running"` | Supervisor process alive. |
| `DeployStatus.STOPPED` | `"stopped"` | Cleanly stopped via `stop`. |
| `DeployStatus.DEAD` | `"dead"` | PID no longer alive (crashed without cleanup). |

### `DeployEntry`

`class DeployEntry(BaseModel)` — one registry row describing a deployed pipeline.

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `name` | `str` | — (required) | Pipeline name (the registry key). |
| `pid` | `int` | — (required) | OS process id of the supervisor. |
| `dir` | `str` | — (required) | Resolved project directory. |
| `session` | `str` | — (required) | e.g. `"crawfish/triage-bot"` — never carries a secret. |
| `backend` | `str` | `"daemon"` | `daemon` or `tmux`. |
| `schedule` | `str \| None` | `None` | Cron / interval string, or `None` for continuous. |
| `status` | `DeployStatus` | `DeployStatus.RUNNING` | Current lifecycle label. |
| `started_at` | `float` | now (UTC epoch seconds) | Set at construction. |
| `log_path` | `str` | `""` | Where the daemon's stdout/stderr is appended. |
| `version` | `str` | `"0.1.0"` | Deploy schema version. |

### `DeployRegistry`

`class DeployRegistry` — Store-backed registry of deployed pipelines.

```python
DeployRegistry(store: Store, *, org_id: str = "local")
```

| Method | Signature | Returns |
| --- | --- | --- |
| `register` | `register(entry: DeployEntry) -> None` | Upsert a row. |
| `get` | `get(name: str) -> DeployEntry \| None` | One row, or `None`. |
| `entries` | `entries() -> list[DeployEntry]` | All rows, sorted by name. |
| `set_status` | `set_status(name: str, status: DeployStatus) -> None` | Update status only. |
| `remove` | `remove(name: str) -> None` | Delete a row. |
| `reconcile_liveness` | `reconcile_liveness() -> list[str]` | Mark vanished-PID rows `DEAD`; return their names. |

### `Supervisor`

`class Supervisor` — the always-on loop (schedule → fire → record) with ledger-backed
resume. Construct it directly; `deploy` spawns it in a detached process for you.

```python
Supervisor(
    name: str,
    store: Store,
    run_fn: RunFn,                       # Callable[[RunContext], None]
    *,
    schedule: str | None = None,
    org_id: str = "local",
    version: str = "0.1.0",
    backend: str = "command",
    secrets: Sequence[str] = (),
)
```

| Method | Signature | Behaviour |
| --- | --- | --- |
| `due` | `due(now: datetime) -> bool` | Whether a cycle should fire at `now` (always, if no schedule). |
| `run_cycle` | `run_cycle(now: datetime \| None = None) -> str` | Run one cycle; record `RunInfo` + ledger state; return the run id. A raised `run_fn` is caught, recorded as failed, and the supervisor survives. |
| `reconcile` | `reconcile() -> dict[str, list[str]]` | On (re)start, resume/retry orphaned runs via the ledger. |
| `process_items` | `process_items(items, handler) -> list[str]` | Process fan-out items exactly once across restarts; skip those already `DONE`. |
| `serve` | `serve(*, max_cycles=None, now_fn=None, sleep_fn=None, stop_flag=None) -> int` | Block in the loop; return the number of cycles fired. Seams make it testable in zero real time. |

### `deploy`

```python
def deploy(
    project_dir: str | Path,
    *,
    name: str,
    store: Store,
    schedule: str | None = None,
    backend: str = "daemon",
    spawn: Spawner | None = None,
    org_id: str = "local",
) -> DeployEntry
```

Validate the schedule, spawn the detached `craw _supervise` child (argv carries only name +
dir + optional schedule — never a secret), write the registry entry, and return it. When
`schedule` is omitted, the project's own declared `TRIGGER` / `SCHEDULE` (in its
`pipeline.py`) is used. `spawn` is an injectable `Callable[[list[str], Path, Path], int]`
returning a PID. See [ADR 0009](../architecture/decisions/0009-deploy-detached-daemon-over-tmux.md).

### `stop`

```python
def stop(
    name: str,
    *,
    store: Store,
    org_id: str = "local",
    kill: Callable[[int], None] | None = None,
) -> bool
```

Signal the deployed process (`SIGTERM` by default) and set its registry status to
`STOPPED`. Returns `True` if an entry was found, `False` otherwise. `kill` is injectable
for tests.

### `PipelineStatus`

`class PipelineStatus(BaseModel)` — one row in the `craw manage` view: a registry entry
joined with its run state.

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `name` | `str` | — (required) | Pipeline name. |
| `status` | `str` | — (required) | `running` / `stopped` / `dead`. |
| `pid` | `int` | — (required) | Supervisor PID. |
| `dir` | `str` | `""` | Project directory. |
| `schedule` | `str \| None` | `None` | Cron / interval string. |
| `uptime_s` | `float` | `0.0` | Seconds since `started_at`. |
| `last_run_status` | `str \| None` | `None` | Status of the most recent run. |
| `last_run_ago_s` | `float \| None` | `None` | Age of the most recent run. |
| `next_fire` | `str \| None` | `None` | Next scheduled fire time (`%H:%M`). |
| `cost_today_usd` | `float` | `0.0` | Sum of today's run costs. |
| `log_path` | `str` | `""` | Daemon log path. |
| `runs` | `list[RunInfo]` | `[]` | Run history, newest first. |

### `manage_list`

```python
def manage_list(
    store: Store, *, org_id: str = "local", now: datetime | None = None
) -> list[PipelineStatus]
```

Build the management view for every deployed pipeline. Reconciles liveness first (marks
dead PIDs), then joins each registry entry with its run-info history for uptime, last run,
next fire, and today's spend.

### `format_table`

```python
def format_table(rows: list[PipelineStatus], *, show_dir: bool = False) -> str
```

Render the rows as a fixed-width text table. Columns: `NAME STATUS UPTIME LAST RUN NEXT
$TODAY`. With `show_dir=True` a `DIR` column is appended (home prefix abbreviated to `~`).
Empty input returns `"no deployed pipelines (use \`craw deploy\`)"`.

### `restart_target`

```python
def restart_target(
    name: str,
    *,
    store: Store,
    org_id: str = "local",
    spawn: Spawner | None = None,
) -> bool
```

Stop `name`, then re-deploy it from its recorded `dir`, `schedule`, and `backend`. Returns
`False` if no such entry, `True` on success.

### `CronSchedule` (and `Cron`)

`class CronSchedule` — a minimal five-field cron evaluator (`m h dom mon dow`). `Cron` is
an alias for the same class (`Cron = CronSchedule`).

```python
CronSchedule(expr: str)            # raises ValueError unless expr has 5 fields
.matches(dt: datetime) -> bool     # True if dt (to the minute) satisfies the schedule
.next_after(dt: datetime) -> datetime   # first matching minute strictly after dt
```

Supports `*`, `*/n`, `a,b`, `a-b`, and exact values. Day-of-week `0-6`, Sunday = 0. When
both day-of-month and day-of-week are restricted, a tick matches if *either* matches.
`next_after` searches ≤366 days and raises `ValueError` if nothing matches.

### `Trigger`

`class Trigger(ABC)` — base for anything that can fire a pipeline run. Attributes `id: str`,
`kind: str`. Abstract method:

```python
def describe(self) -> dict[str, JSONValue]    # JSON-serialisable description
```

### `CronTrigger`

`class CronTrigger(Trigger)` — fire a run on a cron `schedule`.

```python
CronTrigger(schedule: str)
```

Sets `id` (a fresh `new_id`), `kind = "cron"`, and `schedule`. `describe()` returns
`{"id", "kind", "schedule"}`.

### `WebhookTrigger`

`class WebhookTrigger(Trigger)` — fire a run from an inbound HTTP POST to `path`.

```python
WebhookTrigger(path: str, secret_ref: str | None = None)
```

Sets `id`, `kind = "webhook"`, `path`, and `secret_ref`. **`secret_ref` is the *name* of an
environment variable** holding the shared secret, never the value — so it is safe to
serialise. `describe()` returns `{"id", "kind", "path", "secret_ref"}`.

### `verify_webhook`

```python
def verify_webhook(secret: str, payload: bytes, signature: str) -> bool
```

Compute `HMAC-SHA256(secret, payload)` as lowercase hex and compare it to `signature` in
constant time (`hmac.compare_digest`) to avoid timing oracles. The caller resolves `secret`
from the trigger's `secret_ref` environment variable.

### `BuildPlan`

`class BuildPlan(BaseModel)` — a summary of what `craw build` will produce.

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `image` | `str` | — (required) | Image tag, `<name>:<version>`. |
| `base_image` | `str` | — (required) | e.g. `python:3.11-slim`. |
| `python_version` | `str` | `"3.11"` | `DEFAULT_PYTHON_VERSION`. |
| `lock_present` | `bool` | `True` | Whether `crawfish.lock` is installed. |
| `steps` | `list[str]` | `[]` | Human-readable build steps. |

### `generate_containerfile`

```python
def generate_containerfile(
    manifest: ProjectManifest,
    *,
    python_version: str = DEFAULT_PYTHON_VERSION,   # "3.11"
    lock_present: bool = True,
) -> str
```

Return deterministic Containerfile text: base `python:<version>-slim`, install pinned
`crawfish.lock` when `lock_present`, copy the project tree, `pip install .`, and
`ENTRYPOINT ["craw", "run"]`. Stable for a given input.

### `plan_build`

```python
def plan_build(
    manifest: ProjectManifest,
    *,
    python_version: str = DEFAULT_PYTHON_VERSION,
    lock_present: bool = True,
) -> BuildPlan
```

Return a `BuildPlan` for the manifest. Image tag is `<name>:<version>`; the `steps` list
mirrors the Containerfile stages (without writing any file).

### `write_containerfile`

```python
def write_containerfile(
    manifest: ProjectManifest,
    dest: str | Path,
    *,
    python_version: str = DEFAULT_PYTHON_VERSION,
    lock_present: bool = True,
) -> Path
```

Write `generate_containerfile`'s output to `dest` and return the path. If `dest` is a
directory, the file is written as `dest/Containerfile`.

---

## Example

Three pure operations, no daemons and no network: parse a cron schedule and compute its
next fire from a **fixed** base time, verify a webhook signature, generate a Containerfile,
and render a `manage` table over synthetic rows.

```python
import hashlib, hmac
from datetime import datetime, UTC

from crawfish.triggers import parse_schedule, verify_webhook
from crawfish.build import generate_containerfile
from crawfish.config import ProjectManifest
from crawfish.manage import PipelineStatus, format_table

# 1) Cron: next fire strictly after a FIXED base datetime (no real clock).
base = datetime(2026, 6, 21, 7, 59, tzinfo=UTC)   # 07:59 UTC
sched = parse_schedule("0 8 * * *")               # 08:00 every day
print(type(sched).__name__, sched.expr)
print("matches base:", sched.matches(base))
print("next fire:   ", sched.next_after(base).isoformat())

# 2) Webhook: recompute the HMAC-SHA256 signature and verify in constant time.
secret, payload = "shh", b'{"event":"push"}'
sig = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
print("verify good: ", verify_webhook(secret, payload, sig))
print("verify bad:  ", verify_webhook(secret, payload, "deadbeef"))

# 3) Deterministic Containerfile for a tiny manifest.
manifest = ProjectManifest(name="triage-bot", version="0.1.0")
print(generate_containerfile(manifest, python_version="3.11", lock_present=False), end="")

# 4) Render the manage view over synthetic rows (no live deployment).
rows = [
    PipelineStatus(name="triage-bot", status="running", pid=4242,
                   schedule="0 8 * * *", uptime_s=3725.0,
                   last_run_status="done", last_run_ago_s=90.0,
                   next_fire="08:00", cost_today_usd=0.42),
    PipelineStatus(name="nightly", status="dead", pid=0,
                   uptime_s=86461.0, cost_today_usd=0.0),
]
print(format_table(rows))
```

??? success "▶ Output"

    ```text
    CronSchedule 0 8 * * *
    matches base: False
    next fire:    2026-06-21T08:00:00+00:00
    verify good:  True
    verify bad:   False
    # Generated by craw build for triage-bot:0.1.0.
    # Do not edit by hand; regenerate with `craw build`.
    FROM python:3.11-slim

    WORKDIR /app

    # Copy the self-contained project and install crawfish + the project.
    COPY . .
    RUN pip install --no-cache-dir .

    # The container is the runnable automation.
    ENTRYPOINT ["craw", "run"]
    NAME          STATUS   UPTIME  LAST RUN    NEXT     $TODAY
    triage-bot    running  1h2m    done 1m ago 08:00     $0.42
    nightly       dead     1d      — —         —         $0.00
    ```
