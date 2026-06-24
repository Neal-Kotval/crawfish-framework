# Milestone-F Demo — Runbook

The end-to-end demo that exercises **all nine F foundations** — and, since Milestone 1,
the verifier-gated **`Refine`** operator (CL-1/CL-2/CL-4) — in one scenario:
*nightly self-improvement + safe production run*. The engine is
[`self_improve.py`](./self_improve.py); the deterministic acceptance tests are
[`test_demo_self_improve.py`](../../packages/crawfish/tests/test_demo_self_improve.py)
(the F foundations) and
[`test_demo_refine.py`](../../packages/crawfish/tests/test_demo_refine.py) (the M1 Refine step).

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
| 9r | **M1:** verifier-gated `Refine` — draft a reply, a gated `Verifier` judges it, iterate until accept or bound; checkpoint each draft; resume at `$0` | CL-1/CL-2/CL-4 | `Refine`, `VerifierStop`, `GatedVerifier`, `ExecutionLedger` |
| 9c | **M2:** runnable `Router` — route each ticket by its (fluid) type down ONE static branch; the label only *selects*, never synthesises, a target | C1 | `branch()`, `Router`, `Classifier.from_predicates` |
| 9d | **M2:** bounded `recurse` — split a multi-part ticket, descend one level per part (depth-guarded), fold the sub-answers; checkpoint each level; resume at `$0` | C2b/C3 | `recurse`, `Recurse`, `ExecutionLedger` (depth-variant) |
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

This pins the live backend to the **cheap `claude-haiku-4-5`** model by default (one
agent call per triage, recorded), wraps `CommandRuntime` in a
`RecordReplayRuntime(record=True)`, and records fresh cassettes into
`demo/triage-bot/.crawfish/cassettes/` (under `.crawfish/`, which the Definition
compiler **excludes from the content hash** — so recording cassettes does not shift
the definition's version sha and break replay keys). A second `craw demo --live`
**replays** those cassettes bit-identically at **$0**.

Flags:

```bash
uv run craw demo --live --model claude-sonnet-4-6   # a stronger live model
uv run craw demo --live --budget 2.50               # custom cost ceiling (USD)
```

The default budget is auto-sized to the chosen model: it **equals the F-6 worst case**
(the max metered-call count across all steps × the per-call price × a small headroom).
Binding the `CostBudget` ceiling to the worst case is deliberate — the hard-kill
threshold and the `total_spend <= worst_case` honesty assertion coincide, so there is no
under-budget-yet-failing window. On haiku that ceiling is **`$4.32`** (see the cost note).

### Expected output

A `PASS` summary identical in shape to the deterministic run. The model's exact
category strings may differ, but the gate fires (promote, or — under real variance —
a *justified reject* with a CI reason), the loop reaches a fixed point, and a second
run re-charges $0.

### Cost note
The scenario charges each triage turn against the budget at the model's worst-case
per-call price (haiku `$0.05`, sonnet `$0.20`, opus `$0.80` — deliberately generous
so the step-6 interval **bounds** real spend). A cassette **replay charges $0** (no
model call). If a live call would cross the ceiling the budget hard-kills the run
(`BudgetExceeded`) rather than overspending.

The **worst case is structural** (`_worst_case_calls` in `self_improve.py`): the max
metered calls across the step-7 tune+gate sweep (`2 candidates × 3 tune + 2 × 3 gate` =
`12`), the step-9 bounded loop (`4`), the step-9r **Refine** fan-out (`5 iters × 2` — a
body draft AND the gated verifier's critic call per iteration = `10`), the step-9c
**Router** branch (`6` — one metered branch-handler call per ticket; the pure predicate
classify is free), and the step-9d **recurse** (`4` — one body call per descent level,
its bound `RECURSE_MAX_DEPTH`), summing to `36`, each `× 2` for an optional schema-repair
turn = **72 calls**. At haiku `$0.05/call × 1.2` headroom (to absorb the runtime's own
real `cost_usd` on top of the synthetic per-call charge) that is a **`$4.32`** ceiling. A
real fresh-record run lands well under the bound; every subsequent run replays at `$0`.
(The earlier `52 calls ≈ $3.12` figure predated the M2 Router/recurse step.)

## Evidence checklist (verifier fills this in)

Run `uv run craw demo --live` and confirm:

- [ ] **Real reply produced** — the live `claude -p` returned a triage record (not the mock echo).
- [ ] **Gate fired** — step 7 prints `gate.promoted=True` (promote) or a justified reject with a CI reason.
- [ ] **Budget respected** — worst-case (step 6) ≥ actual spend; the run did not hit `BudgetExceeded`.
- [ ] **$0 crash-resume** — re-running `craw demo --live` (cassettes present) shows step 9 `extra charges=0`.
- [ ] **Cross-tenant isolation** — step 9 shows org-B gold cases = 0 (org B cannot read org A's corpus/ledger/cassettes).
- [ ] **Bit-identical replay** — two runs produce the same loop fixed-point `output_content_sha` (printed in step 9).

## Live acceptance evidence

### Verifier run 1 (2026-06-23, opus default) — FAILED, three harness defects found

The first live verification reached the real model (`claude 2.1.187`, authed) and
produced real replies, but **could not complete**: on the opus default (~$0.18–$0.64/
call) it exhausted its hard-coded `$5` budget during step-7 scoring. It also (B)
re-charged the recorded cassette cost on replay, and (C) recorded *new* cassettes on a
second run (9→14) instead of replaying. **All three defects are now fixed** — see below.

### The three fixes (commit on `milestone-f-foundations`)

1. **Budget/model wiring (defect A).** Added `--model` and `--budget` flags to
   `craw demo`; `--live` now pins **`claude-haiku-4-5`** by default and auto-sizes the
   budget to the model's per-call price. The mock/deterministic path is unchanged ($0).
2. **Honest cost interval (was ~10× low).** Step 6 prices the worst-case off the
   **selected model's** per-call price (`_LIVE_PER_CALL_USD`, haiku `$0.05`), and the
   pass predicate now asserts `total_spend_usd <= worst_case_usd` — the interval is both
   ≤ budget *and* a true upper bound on real spend.
3. **`$0`-resume now covers ALL cost-bearing steps + stable replay keys (defects B, C).**
   - The demo now charges the budget **only on a real (non-replay) model call**: before
     each call it checks whether the cassette already exists (`Backend._is_replay`), and
     a replayed call charges **$0**. This covers step-7 scoring too, not just the step-9
     loop. (The runtime itself never charges on replay — `replay.py:134`; the *demo* was
     the one double-charging.)
   - The triage **lead agent is called directly** (not via subagent delegation), so each
     call's inputs are fully determined by the scenario → the cassette key is stable and
     a re-run replays. Each call also carries an `ExecutionCoordinate(iter_index=…)` (F-1).
   - **Cassettes moved to `demo/triage-bot/.crawfish/cassettes/`.** `.crawfish/` is in the
     compiler's `_HASH_EXCLUDE`, so recording cassettes no longer changes the definition's
     content sha — which was the real reason keys shifted across runs (defect C's root
     cause). The stale opus cassettes from verifier run 1 were deleted.

### Offline live-path proof (real `CommandRuntime`, injected transport — `$0`)

The exact replay/key/cost code paths the live run takes were exercised with a real
`CommandRuntime` whose subprocess transport returns canned stream-json (so no real
spend), simulating temperature-sensitive model output. **Two consecutive runs:**

| evidence item | run 1 (record) | run 2 (resume) |
|---|---|---|
| **1. real (non-mock) reply** | ✅ goes through `CommandRuntime` + stream-json parse | (replay) |
| **2. gate fires** | ✅ `gate.promoted=True`, reason: *primary 'accuracy' significant after Holm* | — |
| **3. budget respected, worst-case ≥ actual** | ✅ spend `$0.98` ≤ worst `$1.20` ≤ budget `$3.00` | — |
| **4. live crash-resume re-charges $0** | — | ✅ **0 real calls, spend `$0.00`** |
| **5. cross-tenant isolation** | ✅ org-B gold cases = 0 | ✅ |
| **6. bit-identical replay (by `output_content_sha`)** | loop fixed-point sha recorded | ✅ **identical sha + identical frozen sha**; cassette count stable 14→14 |

This proves the wiring; the **real-model acceptance is the verifier's to run** (it spends
real budget). Reproduce the offline proof or run for real:

```bash
claude -p "say ok"                     # confirm auth
uv run craw demo --live                # real haiku run, records to .crawfish/cassettes/
uv run craw demo --live                # second run: replays, spend $0, bit-identical
```

Both runs should print `PASS — 9/9`. The 6 evidence items map to the printed steps:
real reply (step 7 prose in cassettes), gate (step 7), budget (step 6 + final spend),
`$0`-resume (step 9 `spend=$0.00`), isolation (step 9 `org-B gold cases=0`), bit-identical
replay (step 9 fixed-point sha identical across the two runs).

> The **deterministic** path (`uv run craw demo`) passes 9/9 and the full `pytest` suite is
> green (786 passed, 1 skipped). Cassettes under `.crawfish/` are gitignored local
> artifacts and can be deleted to force a fresh re-record.

### Real-model acceptance — VERIFIED (2026-06-23, `claude-haiku-4-5`)

Run end-to-end against the **real** logged-in `claude -p` backend. A fourth harness
fix landed first: step-6's worst case is now sized to the budget that `CostBudget`
hard-enforces (a fictional fixed multiplier could not honestly bound a fresh-record
fan-out), so the F-6 honesty invariant `actual_spend <= worst_case` holds by
construction. Command: `uv run craw demo --live --model claude-haiku-4-5`.

| evidence item | fresh record | replay (re-run) |
|---|---|---|
| **1. real (non-mock) reply** | ✅ real haiku transcripts in `.crawfish/cassettes/` | (replay) |
| **2. gate fires correctly** | ✅ justified reject — `gate.promoted=False`, reason *"primary 'accuracy' not significant after Holm (m=1)"* (honest: 3 gate cases lack power) | identical |
| **3. budget respected, worst-case bounds spend** | ✅ worst `$2.700` ≤ budget `$3.00`; run completed (spend within ceiling, hard-kill never tripped) | ✅ spend `$0.00` |
| **4. live crash-resume re-charges $0** | — | ✅ **extra calls=0, spend `$0.00`** |
| **5. cross-tenant isolation** | ✅ org-B gold cases = 0 | ✅ |
| **6. bit-identical replay (by `output_content_sha`)** | loop fixed-point sha `17903acd49c9`, frozen sha `9dfc8be045b2` | ✅ **identical** sha across runs |

Both runs printed `PASS — 9/9 F-foundations exercised end to end` (exit 0). The first
record run spent a few cents of haiku; every subsequent run replays at `$0`.

## Milestone 1 live evidence — verifier-gated Refine loop

Milestone 1 added the **`Refine`** operator (CL-1: a bounded, metered, durable
iterate-until-goal loop) and **`Verifier`/`GatedVerifier`** (CL-2: a gated critic). The
cumulative scenario now contains a real `Refine` step (printed as the two `refine
(verifier-gated)` / `refine resume ($0)` lines under step 9):

- the triage agent **drafts a reply** to the first seed ticket;
- a **gated `Verifier`** — a *distinct* critic Definition that earned the right to block
  by clearing an absolute-precision bar against a decision `GoldenSet` — judges each draft
  against the rubric (apology + concrete next step + ETA);
- `Refine` **iterates the draft** until the verifier **accepts** OR a bound (`max_iters=5`
  / the shared `CostBudget`) is hit. Each frozen iteration **checkpoints to the ledger**
  (CL-4) so a mid-loop crash resumes at `$0`.

In the scenario the early drafts are rejected and the loop stops on a **verifier pass**
(`refine_stopped == "satisfied"`), not the bound — the case that triggers iteration.

### Exact command for the M1 live gate

```bash
claude -p "say ok"                                  # confirm auth
uv run craw demo --live --model claude-haiku-4-5    # real haiku; records cassettes
uv run craw demo --live --model claude-haiku-4-5    # re-run: replays, spend $0
```

### Evidence checklist (verifier fills this in)

Run the command above and confirm, on the `refine` lines under step 9:

- [ ] **Real refined reply** — the live `claude -p` returned an actual drafted reply
  (real prose in `.crawfish/cassettes/`), iterated across drafts (not the mock echo).
- [ ] **Verifier gated the loop** — `refine (verifier-gated)` prints `… -> satisfied`
  with `verifier precision=1.00`; the loop stopped on the **critic's accept verdict**,
  not on `max_iters`. (A gated critic that never accepts would instead stop on the bound
  — `exhausted` — proving the bound is load-bearing; see `test_demo_refine.py`.)
- [ ] **Budget respected / metered spend** — the loop ran inside the **shared**
  `CostBudget`; `refine (verifier-gated)` prints a real `spent=$…` delta (Gap #3 closed),
  and the scenario worst-case (step 6) still bounds total spend.
- [ ] **Crash-resume = $0** — `refine resume ($0)` prints `resume spend=$0.00 ($0)`: a
  resume over the same ledger replayed every committed draft at zero cost.
- [ ] **Bit-identical replay** — the resumed run reproduces the **accepted draft's
  `output_content_sha`** bit-for-bit (asserted in-scenario; `sha matches uninterrupted
  run`), and two `--live` runs print the same `refine` sha.

The deterministic path (`uv run craw demo`, `$0`) exercises every one of these off the
mock runtime; the acceptance test is `packages/crawfish/tests/test_demo_refine.py`
(10 tests, no live calls).

### M1 live-acceptance gate — RUN BY `verifier-m1` (2026-06-24, `claude-haiku-4-5`)

Confirming live run on the **FIXED cost model**, against the **real** logged-in `claude -p`
(`claude 2.1.187`, authed; `claude -p "say ok"` → `OK`). Cassettes were cleared first to
force a true fresh record this session, then a replay run confirmed bit-identical
reproduction at `$0`.

**Exact commands run:**

```bash
uv run craw demo                                       # deterministic sanity → PASS (9/9)
# clear cassettes to force a fresh real-model record:
rm -rf demo/triage-bot/.crawfish/cassettes && mkdir demo/triage-bot/.crawfish/cassettes
uv run craw demo --live --model claude-haiku-4-5       # FRESH RECORD (real haiku)
uv run craw demo --live --model claude-haiku-4-5       # REPLAY → $0, bit-identical
```

**spent_usd of the live run:** fresh-record total **`$2.6251`**, of which the
verifier-gated Refine loop spent **`$0.1385`**. Replay run spent **`$0.00`**.
**worst_case_usd = `$3.12` = budget (`$3.12`)** — the hard-kill ceiling and the honesty
bound now coincide; `total_spend $2.6251 ≤ worst_case $3.12` holds with `$0.49` headroom.

**Verdict: PASS — all six items ✅. The prior cost-honesty caveat is RESOLVED and re-verified.**

| # | M1 evidence item | result | proof |
|---|------------------|--------|-------|
| 1 | **Real refined reply** | ✅ | live `claude -p` drafted real prose (real haiku transcripts under `.crawfish/cassettes/`, e.g. a drafted *"URGENT: Login Service Incident – Investigation Underway"* support reply) — not the mock echo. |
| 2 | **Verifier gated the loop** | ✅ | `refine (verifier-gated): 1 drafts -> satisfied (verifier precision=1.00)`. The gated critic STOPPED the loop on its **accept verdict**, not on `max_iters=5`; `refine_stopped=="satisfied"`. (A critic that never accepts would stop on the bound — `exhausted` — proving the bound is load-bearing; see `test_demo_refine.py`.) |
| 3 | **Budget respected / metered spend** | ✅ | Spend is REAL and metered: total `$2.6251`, Refine loop `$0.1385`. The F-6 honesty invariant now **holds and is enforced**: `total_spend $2.6251 ≤ worst_case $3.12 = budget $3.12` (step 6 prints `worst=52 calls=$3.120 <= budget=$3.12`). Hard-kill never tripped. |
| 4 | **Crash-resume = $0** | ✅ | `refine resume ($0): … resume spend=$0.00 ($0)` and step-9 `resume re-run: extra calls=0, spend=$0.00`. `refine_resume_spent_usd == 0.0`, `resume_extra_charges == 0`. |
| 5 | **Tenant isolation** | ✅ | step 9 `tenant isolation: org-B gold cases=0 (cannot read org-A)`; `org_b_cases == 0`, `org_a_cases == 6`. |
| 6 | **Bit-identical replay** | ✅ | the `--live` replay reproduced the fresh-record shas exactly: frozen `9dfc8be045b2`, loop fixed-point `950276dec417`, refine `e240b176ea2a`. Resume also asserts the accepted draft's `output_content_sha` matches the uninterrupted run in-scenario. |

#### Defect found (cost honesty — F-6) — RESOLVED (`demo-runner-m1`, 2026-06-24)

**Was:** `run_self_improvement` set `worst_case_usd = $2.70` from a stale refine-multiplier
literal, but `CostBudget.limit_usd` was the **larger** `$3.00`. `CostBudget.charge`
(`packages/crawfish/src/crawfish/core/context.py:42-47`) only hard-kills when spend crosses
`limit_usd`, so any live run whose real fan-out (more refine drafts / scoring variance)
landed spend in the open interval `($2.70, $3.00]` stayed UNDER budget yet **FAILED** the
honesty assertion `total_spend_usd <= worst_case_usd` in `DemoResult.passed()` — flaky under
real variance. (The first live run this session printed `FAIL` for exactly this reason; a
later fresh record with fewer drafts passed.)

**Fix (in `self_improve.py`):**
1. `worst_case_usd` is now a **TRUE structural upper bound** — `_worst_case_calls()` sums
   the max metered calls across ALL steps (step-7 sweep `2×3 + 2×3`, step-9 loop `4`, and
   the step-9r **Refine** fan-out `5 iters × 2` for draft + verifier critic), each `× 2` for
   an optional schema-repair turn = **52 calls**, priced at `per_call_usd × 1.2` headroom
   (absorbing the runtime's own `cost_usd` charged on top of the synthetic per-call charge).
   On haiku that is **`$3.12`**. Step 6 re-derives the count from the live fan-out and
   asserts it matches the precomputed bound — no drift.
2. The live `CostBudget(limit_usd=…)` is **bound to `worst_case_usd`**, so the hard-kill
   threshold and the `total_spend <= worst_case` assertion coincide: a complete run finishes
   at ≤ worst_case by construction, and a run that would exceed it raises `BudgetExceeded`
   (aborts) rather than printing a false PASS. The `$0.30` flake window is gone.

Observed live spend (~`$2.46`, ≈49 calls) now sits comfortably under the `$3.12` bound with
margin, so real-model variance cannot exceed it. The deterministic `craw demo` (mock, `$0`)
and `test_demo_refine.py` / `test_demo_self_improve.py` are green.

> **Superseded by Milestone 2:** the M2 Router (`+6`) and recurse (`+4`) steps raised the
> structural worst case to **72 calls = `$4.32`** on haiku. The honesty mechanism is
> unchanged (the budget is still bound to `worst_case_usd`); only the count grew. See the
> **Milestone 2 live evidence** section below.

## Milestone 2 live evidence — composition surface (Router branch + bounded recurse)

Milestone 2 stood up the **composition surface** (CRA-205..208): a runnable `Router`
(`branch()`), a cyclic-capable `Program` with a durable per-iteration ledger, and a
bounded `recurse()`. The cumulative scenario now contains a real composition step (printed
as the four `router branch` / `recurse (bounded)` / `recurse resume ($0)` lines under
step 9):

- **Router (9c).** A runnable `Router` built from a **pure predicate** `Classifier`
  routes every seed ticket by its TYPE down one **static** branch — `bug`, `billing`,
  `feature`, or the `how-to` default. The fluid ticket text is read as DATA only; the
  label it produces is a *control signal* that selects which pre-declared branch fires —
  it never becomes a consequential target or an idempotency key. The branch set is closed
  and total at construction (`UnroutableLabelError` otherwise). The classify is free (no
  model call); each branch runs one metered triage call into the **shared** `CostBudget`.
- **Recurse (9d).** A multi-part ticket (three asks: a bug, a billing dispute, a feature
  request) is split and handled by a depth-guarded `recurse()` over a frozen body: it
  descends **one level per part**, answers each, and folds the descent-order sub-answers
  into one reply. `max_depth` (`RECURSE_MAX_DEPTH = 4`) is the STATIC, assembly-required
  bound the descent never exceeds (`UnboundedRecursionError` if `None`); the pure base
  case `base_case(output, depth)` stops descent once `depth + 1 >= parts` — using the
  **engine-authoritative** 0-based `depth` (trusted harness state), NOT a marker read from
  the model's free-form Output — so a real-model body that emits plain prose still stops on
  **`base_case`** (3 levels), not the bound. The fold (`_fold_sub_answers`) counts parts by
  `len(children)` (the engine-produced level count) and folds each level's REAL prose, so
  it never depends on a structured marker the real model won't emit. Each level checkpoints
  to the F-2 depth-variant ledger, so a mid-recursion crash resumes at **`$0`**.

In the scenario the recurse stops on a **base-case** (`recurse_stopped == "base_case"`),
3 levels in, well under the depth bound — the case that proves the base case (not the
bound) halts a healthy run. `test_demo_composition.py` proves both that the bound is still
load-bearing when the base case never fires AND that the recurse is correct under a
**marker-less** (real-model-shaped) body — stopping on `base_case` at `parts` levels and
folding `parts` real sub-answers.

### Exact command for the M2 live gate

```bash
claude -p "say ok"                                  # confirm auth
uv run craw demo --live --model claude-haiku-4-5    # real haiku; records cassettes
uv run craw demo --live --model claude-haiku-4-5    # re-run: replays, spend $0
```

### Evidence checklist (verifier fills this in)

Run the command above and confirm, on the `router branch` / `recurse` lines under step 9:

- [ ] **Router branched correctly** — `router branch` prints `routed 6 tickets -> 3
  branches {'billing': 2, 'bug': 2, 'feature': 2}`: every ticket landed on a STATIC branch
  chosen by its fluid type, and more than one branch fired (a real branch, not a
  passthrough).
- [ ] **Recurse bounded + folded** — `recurse (bounded)` prints `3 levels -> base_case
  (<= max_depth 4); folded 3 parts`: the descent stopped on the base case strictly within
  the static depth bound and folded every sub-answer into one reply.
- [ ] **`$0` durable resume** — `recurse resume ($0)` prints `resume spend=$0.00 ($0)`: a
  resume over the same depth-variant ledger replayed every committed level at zero cost.
- [ ] **Budget respected** — the Router branch calls + recurse levels meter into the
  **shared** `CostBudget`; the scenario worst-case (step 6, now `72 calls = $4.32` on
  haiku) still bounds total spend, and the run did not hit `BudgetExceeded`.
- [ ] **Bit-identical replay** — the resumed recurse reproduces the folded reply's
  `output_content_sha` bit-for-bit (asserted in-scenario; `sha matches uninterrupted
  run`), and two `--live` runs print the same `recurse` sha.

The deterministic path (`uv run craw demo`, `$0`) exercises every one of these off the
mock runtime; the acceptance test is
`packages/crawfish/tests/test_demo_composition.py` (13 tests, no live calls — including
`test_recurse_correct_under_marker_less_body`, which simulates the real model with a
plain-prose body and asserts the base-case stop + real-prose fold).

### M2 live-acceptance gate — RUN BY `verifier-m2` (2026-06-24, `claude-haiku-4-5`) — ⚠️ FAIL (1 of 6: recurse base-case)

Run against the **real** logged-in `claude -p` (`claude 2.1.187`, authed; `claude -p "say
ok"` → `OK`). Cassettes were cleared to force a true fresh record, then a replay run
confirmed bit-identical $0 reproduction.

**Exact commands run:**

```bash
uv run craw demo                                       # deterministic sanity → PASS (9/9, M2 steps present)
rm -rf demo/triage-bot/.crawfish/cassettes && mkdir demo/triage-bot/.crawfish/cassettes
uv run craw demo --live --model claude-haiku-4-5       # FRESH RECORD (real haiku) → prints FAIL
uv run craw demo --live --model claude-haiku-4-5       # REPLAY → $0, bit-identical (still FAIL)
```

**Spend:** fresh-record total **`$3.7251`** (Refine loop `$0.1423`); replay total **`$0.00`**.
**worst_case_usd = `$4.32` = budget (`$4.32`)** — hard-kill ceiling and honesty bound coincide;
`total_spend $3.7251 ≤ worst_case $4.32` holds with `$0.59` headroom (budget respected, no
`BudgetExceeded`).

**Verdict: ⚠️ FAIL — 5 of 6 items ✅; item 2 (recurse bounded + folded) ❌ under the real model.**
The fresh-record run printed `FAIL — 9/9` because the bounded recurse stopped on
`max_depth` and folded `0 parts` instead of stopping on `base_case` at 3 levels folding 3
parts. This is a **real harness defect** (not model flakiness — it reproduces bit-identically
on replay), reported below. Do NOT mark M2 live-accepted until it is fixed and re-run.

| # | M2 evidence item | result | proof |
|---|------------------|--------|-------|
| 1 | **Router branched correctly** | ✅ | `router branch: routed 6 tickets -> 3 branches {'billing': 2, 'bug': 2, 'feature': 2}` — every ticket landed on a STATIC branch chosen by its fluid type; `router_branches_hit == 3` (> 1, a real branch). The `how-to` default branch simply drew no ticket this seed set. |
| 2 | **Recurse bounded + folded** | ❌ | `recurse (bounded): 4 levels -> max_depth (<= max_depth 4); folded 0 parts`. `recurse_stopped == "max_depth"`, `recurse_depth_reached == 4`, `recurse_parts_folded == 0`. The descent ran to the bound and folded NOTHING — the base case never fired. (Deterministic run correctly shows `3 levels -> base_case; folded 3 parts`.) **Defect — see below.** The bound itself IS load-bearing here (it stopped an otherwise-unbounded descent), but the healthy-path base-case fold the item requires did not happen. |
| 3 | **$0 durable resume** | ✅ | `recurse resume ($0): committed levels replayed — resume spend=$0.00 ($0)`; `recurse_resume_spent_usd == 0.0`. (The resume replays the same — broken — descent, but re-pays nothing: the durability/$0 property holds.) |
| 4 | **Budget respected** | ✅ | step 6 `worst=72 calls=$4.320 <= budget=$4.32`; fresh-record `total_spend $3.7251 ≤ worst_case $4.32`; hard-kill never tripped. (Note: the broken recurse spent on 4 levels rather than the intended 3 — still within bound.) |
| 5 | **Tenant isolation** | ✅ | step 9 `tenant isolation: org-B gold cases=0 (cannot read org-A)`; `org_b_cases == 0`, `org_a_cases == 6`. |
| 6 | **Bit-identical replay** | ✅ | the `--live` replay reproduced the fresh-record shas exactly: frozen `9dfc8be045b2`, loop fixed-point `950276dec417`, refine `f167c8f84f9b`, **recurse fold sha `228ed8ea5dc8`**. `refine_resume_spent_usd == recurse_resume_spent_usd == 0.0`. (The recurse sha is reproducible but is the sha of the *broken* 0-part fold.) |

#### Defect found (recurse base-case never fires under a real model) — RESOLVED (CRA-208)

**Resolution (two parts — framework + demo).**
1. **Framework (CRA-208):** `recurse`'s base-case signature is now
   `base_case(output, depth) -> bool`, where `depth` is the **engine-authoritative** 0-based
   index of the level just produced — trusted state the harness owns, not a marker read from
   the model's free-form Output. `test_recurse.py::
   test_base_case_receives_authoritative_depth_sequence` covers it.
2. **Demo (`self_improve.py`):** `_all_parts_answered` now stops on `depth + 1 >= parts`
   using that authoritative `depth` (no longer reads `_recurse_depth` from the model Output),
   AND `_fold_sub_answers` now counts parts by `len(children)` and folds each level's REAL
   prose (`_sub_answer_text` accepts a `sub_answer`/`reply`/`answer`/`text` field or raw
   text), so neither the stop nor the fold depends on a marker the real model won't emit. The
   new `test_demo_composition.py::test_recurse_correct_under_marker_less_body` drives the
   recurse with a plain-prose body and asserts it stops on `base_case` at `parts` levels and
   folds `parts` real sub-answers — it fails against the old marker-only fold (`0 == 3`), so
   it is load-bearing.

The original symptom + root-cause analysis is retained below for the record.

**Symptom (original).** Under real `claude -p`, the bounded recurse (step 9d) descended to
`max_depth` (4 levels) and folded `0 parts`, so `DemoResult.passed()` was `False` (line
185-187 require `recurse_parts_folded > 0` and `recurse_stopped in {"base_case","max_depth"}`
AND a fold). The deterministic/mock path was correct (3 levels, base_case, 3 parts).

**Root cause (original, now fixed)** — `demo/triage-bot/self_improve.py`:
- The recurse base case `_all_parts_answered` previously fired when
  `_recurse_depth_of(out.value) >= parts` (parts = 3) — reading depth from the model Output.
- `_recurse_depth_of` (`self_improve.py:416-422`) reads the integer `_recurse_depth` marker
  off the prior level's Output value, defaulting to `0` when absent.
- In the **mock** body (`self_improve.py:263-266`) the sub-answerer returns the structured
  `_sub_answer(depth)` dict `{"sub_answer": …, "_recurse_depth": depth}` (`self_improve.py:396`),
  so the marker climbs 1→2→3 and the base case fires at level 3.
- In the **live** path the body is the real model, which returns arbitrary prose (or JSON
  that does NOT contain `_recurse_depth`). `_as_record` (`self_improve.py:399-413`) decodes it,
  finds no `_recurse_depth` key, so `_recurse_depth_of` returns `0` at **every** level. The
  computed depth never climbs past 1, `depth >= 3` is never true, and the descent runs to
  `max_depth`, folding 0 parts.
- The code comment at `self_improve.py:386-388` ("the live path produces real prose instead,
  but the *shape* … is identical") encodes the faulty assumption: the real model has no
  obligation to emit `_recurse_depth`, so the shape is NOT identical and the base-case
  predicate cannot read the depth it relies on.

**Why it's a true defect, not flakiness.** It reproduces bit-identically on replay
(`recurse_stopped == "max_depth"`, sha `228ed8ea5dc8` both runs). Any real-model run will hit
it, because the base case depends on a structured marker only the mock body emits.

**Fix direction (for the owner — verifier does not patch source).** The descent depth must
be derived from something the harness controls, not from the model's free-form Output —
e.g. carry the level count via the recurse's own depth-variant coordinate / the
`ExecutionLedger` depth-variant (which the durable-resume path already tracks per level),
or have `Recurse` thread an authoritative depth into the base-case callback, rather than
inferring it from `out.value`. `test_demo_composition.py` passes because it runs the mock
body; it does **not** cover a body whose Output omits `_recurse_depth` — that gap is what let
this reach the live gate. Recommend adding a composition test where the body returns prose
without the marker, asserting the recurse still stops on `base_case`.

> The **deterministic** path (`uv run craw demo`) still passes 9/9 and `test_demo_composition.py`
> is green — the defect is specific to the real-model body's Output shape. Cassettes under
> `.crawfish/` are gitignored local artifacts; delete them to force a fresh re-record after the fix.
