# Crawfish vs. hand-rolled Claude — benchmark

- **Mode:** LIVE (`claude -p`)
- **Model:** `haiku`
- **Workload:** 18 synthetic support tickets with ground-truth labels (4 carry prompt-injection payloads)
- **Both paths** run the *same model* over the *same items*, sequentially (crawfish fan-out is sequential today). The only variable is the framework wrapper.

## Bottom line

On a clean bulk-classification task with a capable model, **crawfish and a hand-rolled loop produce near-identical cost, latency, and accuracy.** The framework's value is not happy-path lift — it's the operational guarantees a loop lacks: a hard cost ceiling, typed-output validation with automatic repair, transactional idempotency, crash/resume, and the prompt-injection boundary. Those are demonstrated (deterministically) in the Reliability section below. You adopt crawfish for the runs that *don't* go cleanly, not the ones that do.

## Headline: cost · latency · quality

| Metric | Crawfish | Baseline |
|---|---|---|
| Items processed | 18 | 18 |
| Errors / dead-letters | 0 | 0 |
| Category accuracy | 1.0 | 1.0 |
| Schema-valid rate | 1.0 | 1.0 |
| Injection resisted | 4/4 | 4/4 |
| Total cost (USD) | 0.764051 | 0.754272 |
| Input tokens | 668504 | 666122 |
| Output tokens | 6280 | 5277 |
| Wall-clock (ms) | 160550.7 | 147104.5 |
| Mean latency/item (ms) | 8919.0 | 8172.5 |
| Extra calls (REPAIR) | 0 | 0 |

## What the framework adds (and what it doesn't)

**Quality.** No happy-path edge on this run: both paths hit accuracy 1.0/1.0 and resisted 4/4 vs 4/4 injections — a capable model (haiku) already shrugged off these inline injections without the fence. The framework's quality value is therefore **insurance, not lift**: the typed schema + REPAIR catch the malformed/steered reply *when* it happens (see the deterministic Reliability section), and the boundary is defense-in-depth that matters more with weaker models, longer data, or stronger attacks. On a clean task with a strong model, you pay for guarantees you didn't end up needing.

**Cost.** Roughly equal per item, +1 metered call whenever REPAIR fires (0 extra call(s) here). The framework buys correctness at the cost of an occasional re-prompt, capped by the budget ceiling. Note the absolute input-token count (668,504 across 18 items) is dominated by the ~37k-token context the local Claude Code install loads on *every* `claude -p` call (CLAUDE.md, skills, MCP) — both paths pay it equally, so the comparison holds, but raw-API numbers would be far lower.

**Latency.** ~Equal. Crawfish does **not** win on wall-clock today: fan-out is a sequential `for` loop (`packages/crawfish/src/crawfish/batch.py:107`), so there is no parallelism advantage yet. Per-item overhead (validation, ledger writes) is small relative to a model call. Parallel fan-out is a Phase-2 item.

## Reliability (deterministic, model-free)

- **Malformed-output recovery:** crawfish recovered via one REPAIR re-prompt → `{'category': 'bug', 'severity': 'high', 'summary': 'Recovered triage.'}`. The hand-rolled baseline has no repair path and keeps the garbage.
- **Hard cost ceiling:** with a budget set to ~40% of full-batch cost ($0.004454), crawfish stopped after 4 of 12 items (`budget_exceeded=True`). A hand-rolled loop has no ceiling and bills the whole batch.
- **Transactional idempotency:** first run did work on 10/10 items; a full re-run did work on 0 (consequential sinks fire at most once). A naive loop would redo all 10.
- **Crash / resume:** a Run persisted as `done` was rebuilt from the Store after a simulated restart (`resume_ok=True`). Baseline: none — progress is lost on crash.

## Context management (deterministic, model-free)

| Strategy | Tokens before | Tokens after | Reclaimed | Turns before→after | Fluid summary stays tainted |
|---|---|---|---|---|---|
| max_tokens | 940 | 282 | 658 | 20→6 | False |
| linear_compact | 940 | 290 | 650 | 20→7 | True |
| exponential_compact | 940 | 384 | 556 | 20→9 | True |

_Deterministic, model-free compaction. A compacted fluid turn stays tainted (no silent privilege escalation). The baseline has no windowing — it overflows._

## Learning (eval gate — mechanism only)

- Baseline scores: `{'accuracy': 0.8, 'valid_rate': 1.0}`
- A **regressed** candidate (accuracy 0.65) → promoted: `False` (correctly blocked).
- An **improved** candidate (accuracy 0.90) → promoted: `True` (allowed).

_The eval gate (crawfish.eval.gate_against_baseline) blocks a regression from replacing a working agent. The full LearningLoop.improve() cycle that drives this from trajectories is built but NOT yet wired to live pipelines (Tuner knobs unexposed) — so this demonstrates the gate, not autonomous improvement._

## Honest caveats

1. **Sequential fan-out** — no latency win vs a naive loop yet (Phase 2).
2. **Learning is not autonomous** — the eval *gate* ships and is demonstrated; the `LearningLoop.improve()` cycle that would drive it from real trajectories is not wired to live pipelines (Tuner knobs unexposed).
3. **Token accounting** — crawfish's ledger records `cost_usd` but not token counts; tokens here are parsed by the harness from `claude -p` stream-json.
4. **Mock vs live** — run `--live` for real cost/quality. The mock is for validating the harness and illustrating the boundary effect.
