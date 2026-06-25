---
name: crawfish-new
description: Author a new Crawfish component from a template (definition/pipeline/source/sink/tool/observer/policy/mcp). Writes files — user-invoked.
disable-model-invocation: true
allowed-tools: Bash
---

Run `craw code new $ARGUMENTS --json` and report the created files and the secret-shaped
lint verdict. This wrapper adds no logic — the CLI is the one execution path.
