---
name: crawfish-map
description: Emit the whole-project component graph — flow-tagged IO, topology, consequential sinks (read-only).
allowed-tools: Bash
---

Run `craw code map $ARGUMENTS --json` and report the component graph: nodes with
flow-tagged typed IO, the pipeline/dependency edges, and the consequential sinks (shown as a
static-only kind, never a destination). Read-only — this wrapper adds no logic.
