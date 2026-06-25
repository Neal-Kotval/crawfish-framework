---
name: crawfish-sync
description: Reconcile the authored tree with discovery and run the assembly gate (read-only).
allowed-tools: Bash
---

Run `craw code sync $ARGUMENTS --json` and report the component map, any drift or load
errors, and the assembly-gate result. Read-only — this wrapper adds no logic.
