# Benchmark: Claude **with crawfish** vs. Claude **alone**

A head-to-head over a synthetic bulk workload. Both paths call the **same model**
through the **same `claude -p` backend** over the **same items** — the only variable is
whether crawfish wraps the call.

- **Crawfish path** — a typed `Run` per item via `CommandRuntime`, with a `CostBudget`
  ceiling, typed-output validation, a `REPAIR` re-prompt on malformed output, the
  fluid-data prompt-injection boundary, and an event ledger in the `Store`.
- **Baseline path** — the obvious hand-rolled control: a sequential loop that shells the
  same `claude -p` per item with the ticket inlined, then best-effort-parses the reply.
  No typed boundary, no validation/repair, no budget, no ledger.

## Run it

```bash
# Free, deterministic dry run (simulated claude -p) — validates the whole harness:
uv run python -m bench.run --mock

# Real run against your local claude CLI (costs money):
uv run python -m bench.run --live --n 8 --model haiku
```

Output: a console summary + a full Markdown report at `bench/RESULTS.md`.

## Optimized three-way benchmark (`bench3.py`)

The first benchmark showed the framework *spine* adds no happy-path lift on a one-call
task. `bench3.py` answers the follow-up — *can the framework be made faster/cheaper using
its features?* — by pitting two **new, tested framework capabilities** against naive
single-model loops on a harder task:

- **Parallel fan-out** — `Batch(concurrency=N)` now runs items under a bounded semaphore
  with ordered results and the shared `CostBudget` enforced under concurrency
  (`packages/crawfish/src/crawfish/batch.py`, tests in `test_batch_parallel.py`).
- **Confidence-gated cascade** — `EscalatingRuntime` runs a cheap primary model and
  escalates only the unsure tail to a strong model, over one `CommandRuntime`
  (`runtime/escalate.py`, tests in `test_escalate.py`).

```bash
uv run python -m bench.calibrate --model haiku          # probe: does the cheap model slip?
uv run python -m bench.bench3 --primary haiku --strong sonnet --concurrency 8
```

Live result (`bench/RESULTS_3WAY.md`): crawfish reached **strong-model quality (1.0) at
the cheap-model price (~64% under sonnet-only) and ~2.5–5× faster** than the naive loops.
Honest finding: modern Claude models are at the quality ceiling for classification, so the
cascade rarely escalates — the win is **cost + speed at equal quality**, not a quality
jump. See the report's "Honest notes".

## Authoring benchmark — base Claude Code vs *craw code* (`authoring/`)

The benchmarks above measure the **operate** layer (the crawfish *runtime* over a bulk
workload). [`authoring/`](authoring/README.md) is the complement: does **craw code** (the
authoring skills + `craw code` CLI/jail/gate) help a Claude Code agent *write* a safe,
correct project? Pilot finding (`authoring/RESULTS.md`): the craw arm keeps a consequential
**sink target static** (injection-safe) where the base arm leaves it fluid, and lifts
gate-clean `pass@1` — for a modest authoring-time cost premium. Design + features roadmap:
[`docs/dev/craw-code/03-BENCHMARKS.md`](../docs/dev/craw-code/03-BENCHMARKS.md).

## What it measures

| Dimension | How |
|---|---|
| **Cost** | `total_cost_usd` + input/output tokens parsed from `claude -p` stream-json |
| **Latency** | wall-clock + mean per-item (both paths sequential) |
| **Quality** | category accuracy vs ground truth; schema-valid rate; injection resistance |
| **Reliability** | REPAIR recovery · hard budget ceiling · transactional idempotency · crash/resume |
| **Context mgmt** | deterministic token reclamation across `MaxTokens`/`LinearCompact`/`ExponentialCompact` |
| **Learning** | eval-gated promotion (regression blocked, improvement allowed) |

## Honest caveats (also printed in the report)

1. **Sequential fan-out.** Crawfish's batch is a `for` loop today
   (`packages/crawfish/src/crawfish/batch.py:107`) — so it does **not** win on wall-clock
   vs a naive loop yet. Parallel fan-out is Phase 2.
2. **Learning is not autonomous.** The eval *gate* ships and is demonstrated; the
   `LearningLoop.improve()` cycle that would drive it from real trajectories is built but
   not wired to live pipelines (Tuner knobs unexposed).
3. **Token accounting.** Crawfish's ledger records `cost_usd` but not token counts;
   tokens here are parsed by the harness directly from stream-json.
4. **Mock ≠ live.** The mock models the one behavioural delta we expect (the boundary
   resisting injection) so the plumbing and reliability/context/learning sections are
   real — but treat mock accuracy/cost as illustrative until you run `--live`.

## Files

- `synthetic.py` — 18 labelled tickets (4 carry prompt-injection payloads)
- `task.py` — the shared single-agent classifier `Definition` + the baseline prompt
- `transport.py` — real + simulated `claude -p`, stream-json cost/token parsing
- `paths.py` — `run_crawfish` / `run_baseline`
- `scenarios.py` — the deterministic reliability / context / learning demos
- `run.py` — orchestrator + report renderer
