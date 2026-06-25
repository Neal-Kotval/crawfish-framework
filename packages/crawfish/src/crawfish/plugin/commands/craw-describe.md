---
name: crawfish-describe
description: Describe a Definition — typed IO, capabilities by kind, cost band (read-only, redacted).
allowed-tools: Bash
---

Run `craw code describe $ARGUMENTS --json` and report the Definition's typed IO, its
capabilities (surfaced as kind, never a destination or a secret reference), and the cost
band. Read-only — this wrapper adds no logic.
