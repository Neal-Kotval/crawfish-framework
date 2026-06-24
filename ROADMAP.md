# Roadmap

Where Crawfish is and where it's going. This is the public, outside-in view; it
describes capabilities, not internal tracker IDs. For claimable work and discussion,
see the project's GitHub **Issues** and **Discussions**.

<!-- TODO(maintainer): link the real Issues/Discussions URLs once the repo slug is set
     (e.g. https://github.com/Neal-Kotval/crawfish/issues). -->

## Phase 1 — the local trust loop (shipped)

Crawfish runs an end-to-end pipeline locally with **no hosted dependency and no API
key** (a mock runtime drives the demo). The loop:

> A multi-item **Source** fans out → a **Definition** team runs per item via `claude -p`
> → an **Aggregator** reduces → a **Router** branches → a **Sink** writes.

…and it's **typed, versioned, and benchmarked**, with retries, a dead-letter queue, and
crash-resume. What's in the box today:

- **Authoring as directories** — a directory compiles to a typed Definition; single- and
  multi-agent teams; MCP tool access; pluggable context-window management.
- **Typed structural IO + versioning** — structural type compatibility (never string
  equality), freezable/versioned artifacts and lockfiles.
- **Three swappable seams** — `AgentRuntime`, `Store` (WAL SQLite: tenancy, idempotency,
  event ledger), `ArtifactStore`. The product model imports protocols, never backends,
  so cloud + scale stay a driver swap, not a rewrite.
- **The full node set** — Source, Sink (idempotency, approval gate, static targets),
  Filter/Router/Classifier, Aggregator, Memory, durable Run.
- **Pipelines** — fan-out/fan-in Batch, rule-based scheduling, execution-state ledger,
  retries + dead-letter + replay.
- **Measurement & knowledge** — metrics, rubrics, benchmarks against golden sets, eval
  data lifecycle, cost preview + budgets, a streaming run inspector.
- **Operate, observe & integrate** — `craw deploy` (always-on detached supervisor,
  auto-restart, ledger resume), an observer primitive (rules + LLM judge), `craw manage`,
  a loopback-only `craw visualize` dashboard, `craw export --claude-code`, and a
  configurable project structure with `craw doctor`.
- **Security spine** — fluid (untrusted) inputs reach the model as data, never
  instructions; consequential Sink targets and idempotency keys are static-only; secrets
  resolve by reference and are never logged or in-prompt.
- **Ship surface** — `pip install` → `craw init` → a 5-minute zero-key demo;
  `craw build` → container; a MkDocs docs site; an API-stability contract (stable /
  experimental / deprecated tiers + semver).

## The agent language — foundations landed (in progress)

Phase 2 includes a larger bet: an **agent language** where composition operators
(Refine, Program, Quorum, Escalate) and a Tuner make agents self-improving over your
data. The first slice — the *foundational primitives* those operators stand on — has
landed. These are substrate contracts, **not** the headline operators (which are still
ahead):

- **A canonical Output content hash** — one content-identity primitive every ledger and
  replay path keys off.
- **An execution-coordinate cassette key** — record/replay now distinguishes each re-run
  of a leaf (sample, iteration, visit, depth), and folds tenancy into run identity.
- **A loop/program ledger** — per-`(item, edge, visit)` and per-recursion-depth resume
  with deterministic loop identity, so resuming a loop re-charges \$0 for work already done.
- **One cost model with a composition law** — a three-number interval
  (lower-bound / expected / worst-case) that multiplies along operator nesting.
- **A statistical gate algebra** — paired, variance-aware promotion gates plus a
  fail-closed precision gate for consequential guards, over a shared experiment-design spec.
- **Decode-knob ownership, a determinism tier, a correction corpus, and a Store-backed
  train-mode borrow** — the seams the Tuner and the operators need.

These foundations and their security and migration guarantees are documented in
[`docs/architecture/ARCHITECTURE.md`](docs/architecture/ARCHITECTURE.md) and
[`docs/architecture/SECURITY.md`](docs/architecture/SECURITY.md).

## Phase 2 — themes (next)

Phase 2 turns the local trust loop into something teams run together. Directions we're
exploring (priorities will shift with contributor and user input):

- **Cloud + scale by driver swap** — production `Store` / `ArtifactStore` / runtime
  backends behind the existing protocols, and a managed-deploy path beyond the local
  detached supervisor.
- **A connectors ecosystem** — more first-party and community Sources/Sinks, and a
  smooth path for contributing one (see
  [contributing a connector](docs/guide/contributing-a-connector.md)).
- **A knowledge hub** — promoting the company-brain primitive (built in Phase 1, not yet
  wired) into a shared, reusable knowledge surface across pipelines.
- **Deeper observability** — richer dashboards, alerting, and SLOs over the run-info
  surface.

## Get involved

The best first contribution is a **connector** — a self-contained Source or Sink that
doesn't touch the core seams. Start with
[`docs/guide/contributing-a-connector.md`](docs/guide/contributing-a-connector.md) and
[`CONTRIBUTING.md`](.github/CONTRIBUTING.md). Have an idea for Phase 2? Open a Discussion.
