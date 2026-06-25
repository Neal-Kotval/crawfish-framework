---
name: crawfish-determinism-ledger
description: >
  Determinism discipline and reading the ledger in Crawfish. Load before running,
  evaluating, or debugging a component: iterate on the mock (craw dev), carry randomness in
  --seed, promote to --live under --budget; read results via craw inspect / craw logs and
  the versioned --json surface.
allowed-tools: Read, Grep, Bash
---

# Determinism + reading the ledger

## Mock by default — promote to live deliberately

- **Iterate on the mock.** `craw dev <definition>` runs against `MockRuntime` — no live
  model call, no spend, fully reproducible. This is where you build and debug.
- **`--seed` carries all randomness.** A run's randomness lives in `--seed` (and the
  per-call `decode_seed`), so the same seed replays identically. Two runs with the same
  inputs and seed produce the same result.
- **`--live` is explicit and always budgeted.** Promote to a real model call only when you
  mean to, and **always** under a `--budget`: `craw run <definition> --live --budget 5.00`.
  Never fire `--live` without a budget — the budget ceiling is the cost-governance gate.

The discipline: build and verify on the mock, then promote one deliberate `--live --budget`
run. Never iterate by burning live calls.

## The `--json` contract (the integration surface)

Every `craw … --json` emits a **versioned** envelope:

- `schema` — the major-only tag, e.g. `craw.code.sync.v1` (a minor/additive bump keeps the
  tag stable, so old parsers still match).
- `schema_version` — `{ "major": M, "minor": N }`.

Parse the **cost band** from a run's `--json`:

- `total_usd` — what the run actually spent.
- `expected_usd` — the modelled expectation.
- `worst_case_usd` — the structural worst-case ceiling the budget binds to.

A budget is sized against `worst_case_usd`, not `expected_usd`, so the ceiling can't be
exceeded under live variance.

## Where am I / what happened

- `craw inspect <run>` — the run's structured record (inputs, outputs, cost, lineage).
- `craw logs <run>` — the scrubbed transcript/event log (no secret value ever appears —
  it is redacted through `ScrubbingStore` before the Store write).
- `craw code map` — the whole-project graph: flow-tagged IO, pipeline topology,
  consequential sinks (shown as a static-only **kind**, never a destination).
- `craw code sync` — "is the tree healthy and runnable" (runs the assembly gate).

`Bash` is allowed here because reading the ledger **is** a `craw … --json` call (the
CLI-as-contract). Use it to read state; do **not** use it to fire a `--live` run without a
`--budget`.

## `craw.error.v1` — `retryable` drives the next move

On failure a `craw … --json` call emits the `craw.error.v1` envelope:

```jsonc
{ "schema": "craw.error.v1",
  "code": "fluid_to_static_sink",
  "retryable": false,
  "remediation": "…static, never echoes a fluid value…",
  "detail": { … } }
```

- `retryable: true` — a transient failure (e.g. `tree_busy`). Re-running may succeed.
- `retryable: false` — **stop.** Every **security** rejection is non-retryable
  (`jail_violation`, `fluid_to_static_sink`, `signing_required`, `consent_required`,
  `schema_skew`). Do not loop past it — an injected agent must not be able to retry past a
  security gate. Fix the cause (or get human consent), then proceed.

The `remediation` string is **static**: it never echoes a fluid/tainted input back into your
instruction stream, so reading an error can never re-inject the attack.
