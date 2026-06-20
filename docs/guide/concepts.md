# Concepts

The model behind Crawfish. Each section maps to real public API; see the
[API reference](api-reference.md) for exact signatures.

## The directory model

**An agent is a directory.** You write markdown for the instructions and skills, and
Python for the typed IO, tools, and policies. The compiler reads the directory and turns
it into a typed `Definition`. Here's what it looks for:

| Path | Becomes |
| --- | --- |
| `instructions.md` | the lead/main agent (front-matter = topology, body = prompt) |
| `agents/*.md` | one subagent each (role = filename stem) |
| `definition.py` | typed `inputs`/`outputs`, `dependencies`, `coordination`, `lead` |
| `tools/*.py` | a tool named after the file stem (a callable of that name) |
| `policies/*.py` | module-level `Policy` instances → `DefinitionAssets.policies` |
| `mcp/*.py` | module-level `MCPConnection` instances |
| `skills/*.md` | skill assets |
| `pyproject.toml` | identity (`name`) + version |

Compile with `Definition.from_package(path)` (or `load_definition(path)`). Identity is
**content-derived**: a sha over the directory contents, never the path or a timestamp. So
a directory and its installed package compile byte-identically. The compiler writes a
`definition.lock` for reproducibility. If an agent references a tool, policy, or delegate
that doesn't exist, the broken binding fails at **load time**, not partway through a run.

A compiled `Definition` is `Freezable`. Call `.freeze()` to seal it into an immutable
artifact; mutating a frozen one raises `FrozenError`.

## The pipeline

Bulk work is a pipeline of `Node`s:

```
Source → Filter → Batch(Definition) → Aggregator → Router → Sink
              ├─ fan-out:    one Run per item   (map)
              ├─ Aggregator: N Outputs → one    (reduce)
              └─ Router:      branch by label    (branch)
```

Data flows as `Output`: a frozen, self-describing envelope that carries the value, its
schema, and the id of the node that produced it. Nodes never mutate an Output. A transform
calls `derive` to make a fresh one, leaving the upstream value intact for audit. Adjacent
stages are **type-checked at assembly** (structural `parameters_compatible`), so a mistyped
wire is rejected before any model call.

- **`Source`** is the pipeline's ingress. `fetch()` returns a typed `Output`. A *multi*
  source (`multi = True`) returns a list, and `fan_out` explodes it into one `Output` per
  item, each seeding its own `Run`. The built-ins are `RepoSource` (single) and
  `PullRequestSource` (multi). Both are deterministic and run without network access
  (they're fixture-driven).
- **`Filter`** is a pure, synchronous node that narrows a list `Output` by a predicate
  and preserves order. Factories: `title_contains`, `field_equals`, `field_matches`,
  `limit`.
- **`Batch`** is the assembly point. You wire `Source`s and `Output`s into a `Definition`
  with `.add_input(...)`. A multi source fans out to one `Run` per item, and
  `check_wiring()` type-checks at assembly. The batch's cost ceiling carries onto every
  child `Run`.
- **`Aggregator`** is the fan-in counterpart: it consumes N item `Output`s and emits one.
  The built-in reducers (`collect`, `concat`, `count`, `dedupe`) are pure; a
  `definition_reducer` runs an agent team to reduce, for example to summarize. `fan_in`
  is the barrier that handles partial success: it drops failed or `None` items and
  supports a `quorum`.
- **`Router`** sends an `Output` down one labelled branch, chosen by a `Classifier`.
  Classifiers come in two flavours: `from_predicates` (pure) and `from_definition`
  (agent-backed). The label set is closed and always includes a `default` (dead-letter)
  label, so every item is routable. Unroutable wiring raises `UnroutableLabelError` at
  **assembly time**.
- **`Sink`** is the only place a pipeline performs an external side effect. The built-ins
  are `LinearSink` and `GitHubPRSink`, both dry-run by default and network-free. Three
  invariants keep egress safe (below).
- **`Workflow`** is the top-level deployable: ordered steps with the `Output` threaded
  from stage to stage, adjacency type-checked at assembly. Orchestration state is
  checkpointed to the `Store` after each stage, so a crash mid-workflow resumes from the
  last completed step.

## Runtimes — the swappable agent loop

`AgentRuntime` is the **only** place the model SDK or CLI is touched. Every run goes
through this one interface, which is what makes the backend swappable:

| Runtime | Backend | Key | Cost | Use |
| --- | --- | --- | --- | --- |
| `MockRuntime` | pure function of the request | no | $0 | dev loop, tests, benchmarks |
| `CommandRuntime` | local `claude -p` subprocess | no | your Claude session | real local runs |
| `RecordReplayRuntime` | wraps any runtime; cassettes | no on replay | $0 on replay | snapshot/replay tests |
| `ClientRuntime` | API client | yes | metered | (stub today) |
| `ManagedRuntime` | hosted CMA | yes | metered | (stub today) |

Going from dev to prod is a **runtime swap, not a code change**. `get_runtime(name)`
resolves a runtime by name from `RUNTIME_FACTORIES`.

- **Dev loop.** `MockRuntime` returns deterministic canned text with no model call.
  Iterating on a Definition or a metric never burns budget, and scores never drift.
- **Replay.** `RecordReplayRuntime(inner, cassette_dir, record=True)` records real
  `RunResult`s once, then replays them at zero cost. If a cassette is missing and
  recording is off, it raises `CassetteMiss`. This is what makes `craw dev` and
  `craw test` fast and deterministic.

## The static-vs-fluid prompt-injection boundary

Every `Parameter` carries a `Flow`:

- `Flow.STATIC` is **trusted config**, set once per batch — a repo, a project. It can be
  interpolated directly into the agent's instructions.
- `Flow.FLUID` (the default) is **untrusted per-item data**, like a ticket body. It goes
  *only* inside a clearly delimited, labelled data block, and the instructions tell the
  model to treat that block as data, never as instructions. Static config never mixes
  with fluid data.

The prompt compiler enforces this. `split_inputs` partitions inputs by their declared
flow, and anything unknown defaults to fluid — the safe, untrusted side. This is the
load-bearing prompt-injection defence. A ticket body can't smuggle instructions into the
agent, and because sink targets must be static, a model-influenced value can't redirect
where a write lands.

## Secrets by reference

Credentials are held **by reference only**. Config stores the *name* of an environment
variable, like `"GITHUB_TOKEN"`, never the value itself. The value is resolved at the
egress boundary by `resolve_secret` and injected into a tool's or MCP server's
environment. It never reaches a prompt, the stored config, an `Output`, logs, or
telemetry. An `MCPConnection`'s `auth` field is a secret reference by construction.

## Safe egress — the Sink invariants

A `Sink` is the one place side effects happen. Three invariants keep that safe:

1. **Static-only targets.** Destination slots — repo, project, channel — must be
   `Flow.STATIC`. A fluid target is rejected at construction with `TargetMustBeStaticError`,
   so a prompt can never redirect a write.
2. **Idempotency.** Every write is keyed by a hash of *static config only* plus the batch
   and output identity, never the fluid or model-derived value. Re-running the same batch
   is a no-op, not a duplicate side effect.
3. **Approval gate.** An `always_ask` sink refuses to fire without an explicit approval
   callback, raising `ApprovalRequired`. A run can also suspend durably on approval before
   spending any compute (`requires_approval` → `RunSuspended`).

## Team coordination

A `TeamSpec` carries the multi-agent topology. Coordination leans on Claude's
**hierarchical subagent model** rather than a bespoke message bus. Agents communicate by
delegating in and returning a typed result out:

- **`SINGLE`** is one agent, or several independent agents, with no coordinator.
- **`LEAD`** is a lead that dispatches the roles in its `delegates_to`, then combines
  their typed results. Each subagent result re-enters the lead as **fluid data**
  (`{role}_result`), never as instructions, which preserves both typing and the injection
  boundary.
- **`SEQUENTIAL`** runs agents in declared order; each result threads into the next as
  `prior_result`.

`run_team` executes the topology and returns one `RunResult`. The coordinator is
runtime-agnostic — it works with the mock, so tests stay deterministic.

## The Store and ArtifactStore seams

All persistence goes through the `Store` protocol. The product model imports the
*protocol*, never a concrete backend, so SQLite to Postgres is a driver swap and no raw
SQL appears at any call site. Every row carries an `org_id` tenancy key (defaulted to
`"local"`), so cloud multi-tenancy is also a driver swap, not a schema migration. The
local default is `SqliteStore`. The `Store` backs typed records, KV and working memory,
idempotency claims, and the append-only event ledger that powers the inspector.

- **`Memory`** is a thin `Store`-backed KV and dedup handle, scoped to a
  `(namespace, org_id)` pair. It covers working memory (`get`/`set`), cross-run dedup
  (`already_processed`/`mark_processed`), and an atomic `claim` that wins exactly once per
  id. Because state lives in the `Store`, dedup survives across runs.
- **`ArtifactStore`** (with `LocalArtifactStore` and `offload_if_large`) is the blob seam:
  the local filesystem now, S3 later. Large payloads are offloaded by reference instead of
  carried inline.

## Cost, budgets, and inspection

- **`estimate_cost(definition, items=N)`** is a deterministic dry-run preview. It assumes
  one run per agent per item, prices it from a coarse per-model table, and returns a
  `CostEstimate` — so you see the bill before a single model call. The `mock` model is
  free, so dev and replay preview at $0.
- **`CostBudget`** is the hard ceiling the orchestrator can kill a run on, raising
  `BudgetExceeded`. **`Budget`** layers a warn/stop policy on top (`BudgetState` is
  ok/warn/stopped), and **`CostMeter`** is a live accumulator that tracks remaining
  headroom.
- **`inspect_run(store, run_id)`** derives a `RunReport` — status, cost, latency, tool
  calls, transcript — purely from the Store's append-only event ledger, with no live model
  call. `tail_events` is the poll primitive for live streaming, and `format_report`
  renders a report for the CLI. This is the trust and devtools layer that backs
  `craw inspect` and `craw logs`.

## The measurement loop

`Metric` → `Rubric` → `Benchmark` make quality measurable and comparable across Definition
versions. The eval data lifecycle (`EvalCase`, `GoldenSet`, `LLMJudge`, `capture_case`,
`grade_output`, and `save_baseline`/`load_baseline`/`gate_against_baseline`) lets you
capture real runs as reusable cases, curate versioned golden sets, grade with an
LLM-as-judge, and gate a candidate against a stored regression baseline. All of it is
deterministic under mock and replay. See the [cookbook](cookbook.md) for eval-as-test.
