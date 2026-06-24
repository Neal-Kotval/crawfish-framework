# Crawfish Roadmap — Phase 1 (framework + docs)

Live working plan, mirrored from Linear epic **CRA-98**. Build **top to bottom**;
items in the same milestone parallelize once their blockers are `done`. Researchers
fold best-practice findings into per-feature notes in this directory; the roadmap
reviewer approves a folding before code is written.

Status legend: ✅ done · 🔄 in progress · ⛔ blocked · ⬜ not started

## M0 — Foundation & substrate
*Exit: an empty project runs a no-op pipeline; typed wiring, Store round-trip, and versioning work.*

| # | Issue | Title | Status |
|---|-------|-------|--------|
| 1 | CRA-131 | Project skeleton, packaging, config & CI | ✅ |
| 2 | CRA-99  | Shared core + Store seam | ✅ |
| 3 | CRA-100 | Versioning — Version, freeze, lockfile | ✅ |
| 4 | CRA-132 | Type model & registry (structural typed IO) | ✅ |

## M1 — Definition & runtime
*Exit: a directory compiles to a Definition and runs a single/multi-agent team via `claude -p`, with MCP tools and context compaction.*

| # | Issue | Title | Status |
|---|-------|-------|--------|
| 5 | CRA-101 | Output — typed envelope between nodes | ✅ |
| 6 | CRA-102 | Definition + directory compiler (the heart) | ✅ |
| 7 | CRA-112 | AgentRuntime backends + `craw dev` | ✅ |
| 8 | CRA-135 | Multi-agent team coordination (TeamSpec) | ✅ |
| 9 | CRA-138 | Context-window management (pluggable) | ✅ |
| 10 | CRA-116 | MCP tool access in Definitions | ✅ |

## M2 — Nodes & a Run
*Exit: a single Run executes end to end and produces a typed Output; all IO nodes exist.*

| # | Issue | Title | Status |
|---|-------|-------|--------|
| 11 | CRA-103 | Source — single/multi-item fan-out | ✅ |
| 12 | CRA-104 | Sink — idempotency, approval gate, static targets | ✅ |
| 13 | CRA-105 | Filter — routes / narrows an Output | ✅ |
| 14 | CRA-123 | Memory / state primitive | ✅ |
| 15 | CRA-106 | Run — durable single-task execution + telemetry | ✅ |

## M3 — Pipelines (map / reduce / branch)
*Exit: a multi-stage pipeline with fan-out, fan-in, and branching runs durably with retries.*

| # | Issue | Title | Status |
|---|-------|-------|--------|
| 16 | CRA-107 | Batch — hand-wired pipeline + fan-out | ✅ |
| 17 | CRA-133 | Aggregator (fan-in / reduce) | ✅ |
| 18 | CRA-136 | Router & Classifier (branch) | ✅ |
| 19 | CRA-108 | Batch Executor & Scheduling (rule-based) | ✅ |
| 20 | CRA-134 | Execution-state ledger | ✅ |
| 21 | CRA-122 | Retries, dead-letter & replay | ✅ |
| 22 | CRA-137 | Artifact store | ✅ |
| 23 | CRA-109 | Workflow / Pipeline | ✅ |

## M4 — Measurement & knowledge
*Exit: runs are measured, benchmarked against golden sets, cost-previewed, and inspectable.*

| # | Issue | Title | Status |
|---|-------|-------|--------|
| 24 | CRA-110 | Metrics, Rubrics & Benchmarks | ✅ |
| 25 | CRA-139 | Eval data lifecycle | ✅ |
| 26 | CRA-111 | Company Brain | ⏸️ deferred → Phase-2 hub (CRA-125); built, unwired |
| 27 | CRA-121 | Cost preview + budgets | ✅ |
| 28 | CRA-120 | Run inspector + streaming | ✅ |

## M5 — Authoring, packaging & ship
*Exit: `pip install` → `craw init` → 5-min wow; `craw build` → container; docs complete; tests, secrets, API-stability contract.*

| # | Issue | Title | Status |
|---|-------|-------|--------|
| 29 | CRA-113 | craw CLI + module discovery | ✅ |
| 30 | CRA-118 | First-run zero-key wow | ✅ |
| 31 | CRA-117 | Docs as a product (MkDocs site) | ✅ |
| 32 | CRA-119 | craw test | ✅ |
| 33 | CRA-114 | Secrets v1 + security hardening | ✅ |
| 34 | CRA-115 | Container build/deploy + triggers | ✅ |
| 35 | CRA-124 | API stability, semver & migration | ✅ |

## Phase 1 Hardening — Operate, Observe & Integrate (epic CRA-150)

*Exit: a pipeline goes from "runs once" to always-on — deployed, watched, managed —
plus Claude Code integration and a clearer/configurable structure. Built on the
framework above; cloud/container deploy stays CRA-115/CRA-130.*

| # | Issue | Title | Status |
|---|-------|-------|--------|
| 1 | CRA-154 | Observer events + run-info surface (`ctx.emit`, `ObserverSurface`) | ✅ |
| 2 | CRA-157 | Configurable project structure + `craw doctor` (`[project.paths]`) | ✅ |
| 3 | CRA-151 | `craw deploy` — always-on detached supervisor (auto-restart, ledger resume) | ✅ |
| 4 | CRA-153 | Observer primitive — rule-based + Definition-backed LLM judge | ✅ |
| 5 | CRA-152 | `craw manage` — list/stop/restart/logs over registry+ledger+cost | ✅ |
| 6 | CRA-155 | `craw visualize` — loopback-only dashboard over the run-info surface | ✅ |
| 7 | CRA-156 | Claude Code integration — `craw export --claude-code` | ✅ |

Decisions recorded as ADR 0008 (observer surface as a Store facade) and ADR 0009
(deploy via a detached session-leader daemon, not tmux). Dogfooded end to end in
`demo/` (deploy → observe → visualize → manage) with an integration test.

## Milestone F — Agent-language foundations (Linear project "The Agent Language", shipped)

*Exit: the substrate primitives the agent-language operators (Refine, Program, Quorum,
Escalate, the Tuner) stand on exist, are content-hashed, tenancy-scoped, and
back-compatible. These are foundations — the headline operators themselves are M1+, not
shipped here.*

| # | Issue | Title | Status |
|---|-------|-------|--------|
| F-0 | CRA-193 | `Output` content hash (`output_content_sha`) — the canonical content identity | ✅ |
| F-1 | CRA-194 | Canonical cassette key / `ExecutionCoordinate` + `org_id`/`decode_seed` fold | ✅ |
| F-2 | CRA-195 | Loop/program ledger composite key space + deterministic `compute_loop_id` | ✅ |
| F-3 | CRA-196 | The gate algebra — three gates (relative / paired / fail-closed precision) | ✅ |
| F-4 | CRA-197 | `GoldenSet.from_corrections` + `correction` emission kind (provenance/taint gate) | ✅ |
| F-5 | CRA-198 | Decode-knob ownership + `DeterminismTier` capability | ✅ |
| F-6 | CRA-199 | Cost-model single owner + multiplicative composition law (`CostShape`) | ✅ |
| F-7 | CRA-200 | Store-backed exclusive borrow / train-mode lifetime | ✅ |
| F-8 | CRA-201 | `experiment-design.md` spec + `crawfish.experiment` statistical substrate | ✅ |

These are foundations consumed by the still-to-ship operators and tuner: CL-1/CL-2 (Refine
preflight, fail-closed verifier gate), C2/C3 (loop/program execution), AL-* (the Tuner,
cost-regularized objective), and TS-* (decode/replay seams). Decisions recorded as
ADR 0017 (decode-knob ownership), ADR 0018 (borrow lifetime semantics), and ADR 0019
(content-hash version-bump + migration policy). The normative statistical-conformance gate
is [`docs/architecture/experiment-design.md`](../architecture/experiment-design.md).

## Per-feature research notes

Researcher findings (reviewed before implementation) live alongside this file as
`docs/roadmap/<feature>.md` — e.g. `topo-sort.md`, `wal-concurrency.md`,
`prompt-injection.md`, `content-addressed-store.md`.
