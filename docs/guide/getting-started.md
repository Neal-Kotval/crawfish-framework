# Getting started

Crawfish runs agents over your data in bulk. You write a pipeline as a folder of files —
`Source → Batch → Aggregator → Router → Sink` — and run it locally with `claude -p`.
The dev loop needs no hosted service and no API key.

This page takes you from a clean checkout to a running agent team in a few minutes.

## Install

Crawfish is a `uv` workspace. From a checkout:

```bash
git clone <repo> && cd crawfish-framework
uv sync
uv run craw --version
# crawfish 0.1.0
```

`uv sync` installs the workspace, including the `crawfish` package (editable) and the
`craw` CLI. If your environment already has the dev tools, you can skip it — `uv run
craw --version` is enough to confirm the install.

## How runs execute

The agent loop sits behind one seam, `AgentRuntime`. You pick how runs actually happen:

| Runtime | What it does | Key? | Cost |
| --- | --- | --- | --- |
| `MockRuntime` | deterministic canned responses — the dev/test loop | no | $0 |
| `CommandRuntime` | drives your local `claude -p` subprocess | no | uses your Claude session |
| `RecordReplayRuntime` | records once, replays from cassettes after | no (on replay) | $0 |
| `ClientRuntime` / `ManagedRuntime` | API key / hosted backends | yes | metered |

The dev loop costs nothing. `MockRuntime` is a pure function of the request, so
iterating on a pipeline never spends money and tests stay deterministic. For real runs
you swap in `CommandRuntime`, which uses your local `claude -p`. Going from dev to prod
is a runtime swap, not a code change.

## First run — the no-op pipeline

`craw run` exercises the engine end to end. With no project authored yet it runs a no-op
pipeline, which confirms the `Engine → RunContext → Store` path works:

```bash
uv run craw run
# pipeline ok: 0 output(s)
```

## First real run — `craw dev`

`craw dev` compiles a **Definition directory** and runs its agent team on `MockRuntime` —
no key, no cost. The repo ships an example, `demo/triage-bot`: a lead agent that triages
a support ticket by delegating to a classifier and a summarizer.

```bash
uv run craw dev demo/triage-bot -i project=acme -i ticket_body="login button broken"
```

You'll see the lead's combined result, with the classifier and summarizer results
threaded back in as data (the mock echoes structured input, so the shape is visible):

```text
[lead] processed: {"classifier_result": "[classifier] processed: ...",
                   "summarizer_result": "[summarizer] processed: ...",
                   "ticket_body": "login button broken"}
```

`-i name=value` binds inputs and is repeatable. Note the two kinds: `project` is a
**static** input (trusted config) and `ticket_body` is **fluid** (untrusted per-item
data). That distinction is the prompt-injection boundary — see [concepts](concepts.md).

## Run it for real with `claude -p`

Same Definition, real model — you just swap the runtime. `craw dev` is mock-only by
design, so to run against your local Claude, use the API directly:

```python
import asyncio

from crawfish import CommandRuntime, Definition, RunContext, Run, SqliteStore

definition = Definition.from_package("demo/triage-bot")

async def main() -> None:
    ctx = RunContext(store=SqliteStore())
    run = Run(definition, {"project": "acme", "ticket_body": "login button broken"})
    out = await run.execute(ctx, CommandRuntime())  # drives `claude -p`, no API key
    print(out.value)

asyncio.run(main())
```

`CommandRuntime` shells out to your local `claude` binary (`claude -p`), reusing your
existing Claude session. There's no API key to manage.

## Use the core API directly

The primitives are plain, typed Python. You can drive them without the CLI:

```python
from crawfish import Flow, Parameter, parameters_compatible, SqliteStore, Version

# Typed IO atoms — static (trusted config) vs. fluid (untrusted per-item data)
repo = Parameter(name="repo", type="str", flow=Flow.STATIC)
body = Parameter(name="ticket_body", type="str")  # fluid by default

# Structural type compatibility decides what can wire to what
assert parameters_compatible(repo, body)

# Versioned, freezable artifacts
v = Version(major=0, minor=1, sha="abc")
print(str(v))  # 0.1-abc

# Persistence through the Store seam (SQLite locally, Postgres later — a driver swap)
store = SqliteStore()
store.put_record("definition", "d1", {"name": "clarity"})
```

## What's next

- **[Tutorial](tutorial.md)** — build the triage bot end to end: the directory model,
  compiling, running a team, wiring a `Source → Batch → Sink` pipeline, and measuring
  with a Rubric.
- **[Concepts](concepts.md)** — the directory model, the pipeline, runtimes, the
  prompt-injection boundary, secrets-by-reference, team coordination, and the Store seams.
- **[Cookbook](cookbook.md)** — short recipes (fan-out, fan-in, routing, dedup, retries,
  cost preview, eval-as-test, snapshot/replay).
- **[API reference](api-reference.md)** — the full public surface, auto-generated from
  `crawfish.__all__`.

### The CLI today

The M0 CLI ships `craw --version`, `craw run`, and `craw dev <path> -i name=value`. The
rest of the command surface (`init / install / list / freeze / publish / build / test /
logs / inspect`) is planned. It's marked *coming* where it appears below and isn't
runnable yet.
