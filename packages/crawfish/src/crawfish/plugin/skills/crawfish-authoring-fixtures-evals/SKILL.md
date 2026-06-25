---
name: crawfish-authoring-fixtures-evals
description: >
  Author fixtures/ for craw test and gate on a baseline with craw eval — deterministic,
  mock-by-default, randomness carried in --seed. Load when adding test inputs or an eval
  gate to a Definition.
user-invocable: false
allowed-tools: Read, Grep, Bash
---

# Authoring `fixtures/` & evals

Derived from `docs/specs/craw-code/authoring/fixtures-evals.md`. Golden:
`demo/craw-code-golden/fixtures/`.

`fixtures/` feeds `craw test` (eval-as-test): each `fixtures/*.json` is one input set the
Definition runs against. Evals gate against a saved baseline (`craw eval --baseline`).

## The fixture shape

```json
{"inputs": {"project": "acme", "ticket_body": "the login button does nothing"}}
```

A fixture is `{"inputs": {...}, "expected": <optional>}`. `inputs` maps each declared
`Parameter` name to a value. With no `expected`, a fixture passes when the run executes
cleanly; with `expected`, the Output value must match.

```bash
craw test definitions/<name> --fixtures fixtures
```

## Determinism: mock-by-default

Fixtures run on the `MockRuntime` / the `command` profile's recorded transport, never a live
model, so the suite is deterministic and free. All randomness is carried in `--seed`; the
same seed replays byte-identically. Promote to `--live` only deliberately and always under
`--budget`.

> **Spine rule (determinism-mock-default):** Fixtures run mock-by-default; randomness is
> carried in --seed.

## The eval baseline gate

`craw eval --baseline <ref>` compares per-metric scores against a saved baseline within a
`--tolerance`, and reports the deltas plus the cost band (`total_usd` / `expected_usd` /
`worst_case_usd`). Sinks fire only in eval mode; an optimize/search run never fires a
consequential action. Read `retryable` on a `craw.error.v1` to decide retry-vs-stop — a
security rejection is `retryable:false`.

Fixtures never carry secrets. Reference credentials by name in `.env.example`, never inline.
