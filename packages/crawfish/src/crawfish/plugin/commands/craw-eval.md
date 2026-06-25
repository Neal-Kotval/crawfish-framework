---
name: crawfish-eval
description: Evaluate a Definition against its fixtures on the mock (read-only, deterministic).
allowed-tools: Bash
---

Run `craw code eval $ARGUMENTS --json` and report the evaluation result against the
Definition's fixtures (deterministic, on the mock — no live model call). Read-only — this
wrapper adds no logic.
