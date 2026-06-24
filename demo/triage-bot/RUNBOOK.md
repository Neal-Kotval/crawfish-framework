# Milestone-F Demo — Runbook

The end-to-end demo that exercises **all nine F foundations** in one scenario:
*nightly self-improvement + safe production run*. The engine is
[`self_improve.py`](./self_improve.py); the deterministic acceptance test is
[`packages/crawfish/tests/test_demo_self_improve.py`](../../packages/crawfish/tests/test_demo_self_improve.py).

## What it does (10 steps)

| # | Step | F-feature | Primitive |
|---|------|-----------|-----------|
| 0 | seed TRUSTED corrections (+1 poisoned, quarantined) | F-4 | `emit_correction`, `Provenance.TRUSTED` |
| 1 | open `RunContext(org_id, budget)` | F-1/F-2 | `RunContext` + `CostBudget` |
| 2 | borrow the definition exclusively (train mode) | F-7 | `Definition.mutable` → `crawfish.borrow.mutable` |
| 3 | expose `temperature` as a tunable knob | F-5 | `AgentSpec.temperature` / `resolved_decode` |
| 4 | build the gold set from corrections | F-4 | `GoldenSet.from_corrections` |
| 5 | split tune-set / gate-set | F-8 | `tune_gate_split` |
| 6 | estimate worst-case cost ≤ budget | F-6 | `CostShape.refine` + `compose_cost` |
| 7 | tune temp, run promotion gate (+ winner's-curse shrink) | F-3/F-8 | `paired_gate`, `winners_curse_shrink`, `k_from_alpha` |
| 8 | `freeze()` the winner → new `Version.sha` | F-5 | `Definition.freeze` |
| 9 | bounded refine loop; checkpoint each visit; stop on fixed point | F-0/F-1/F-2 | `output_content_sha`, `ExecutionCoordinate`, `ExecutionLedger` |
| 10 | fire the Sink — permitted **only** because the definition is frozen | security spine | static/frozen-only sink |

## Deterministic run (CI / no credentials, $0)

```bash
uv run craw demo
# or the test:
uv run pytest packages/crawfish/tests/test_demo_self_improve.py -q
```

Runs entirely on the **mock runtime** — no model calls, zero cost, fully
reproducible. Prints a `PASS` summary and exits `0`. This is the path CI gates on.

## Live run (real `claude -p`)

### Credentials
The live path uses the `crawfish.runtime.command.CommandRuntime`, which shells out
to your **logged-in `claude` CLI** (the same binary you use interactively). No API
key env var is needed — just make sure `claude` is on `PATH` and authenticated:

```bash
claude --version        # confirm the CLI is installed
claude -p "say ok"      # confirm you're logged in (should print a reply)
```

### Exact command

```bash
uv run craw demo --live
```

This wraps `CommandRuntime` in a `RecordReplayRuntime(record=True)` so the live run
**records fresh cassettes** into `demo/triage-bot/cassettes/`. A subsequent run can
replay them at zero cost. The budget is intentionally tiny (`$5.00` ceiling, ~12
short classification turns) so a live pass costs cents.

### Expected output

A `PASS` summary identical in shape to the deterministic run (the model's exact
category strings may differ, but the gate fires, the loop reaches a fixed point,
and resume re-charges $0). Exit code `0`.

### Cost note
Keep the budget small. The scenario charges every triage turn against
`CostBudget(limit_usd=5.0)`; if a live model is unexpectedly expensive the budget
hard-kills the run rather than overspending. Worst-case is pre-asserted ≤ budget in
step 6 before any model call.

## Evidence checklist (verifier fills this in)

Run `uv run craw demo --live` and confirm:

- [ ] **Real reply produced** — the live `claude -p` returned a triage record (not the mock echo).
- [ ] **Gate fired** — step 7 prints `gate.promoted=True` (promote) or a justified reject with a CI reason.
- [ ] **Budget respected** — worst-case (step 6) ≥ actual spend; the run did not hit `BudgetExceeded`.
- [ ] **$0 crash-resume** — re-running `craw demo --live` (cassettes present) shows step 9 `extra charges=0`.
- [ ] **Cross-tenant isolation** — step 9 shows org-B gold cases = 0 (org B cannot read org A's corpus/ledger/cassettes).
- [ ] **Bit-identical replay** — two runs produce the same loop fixed-point `output_content_sha` (printed in step 9).
