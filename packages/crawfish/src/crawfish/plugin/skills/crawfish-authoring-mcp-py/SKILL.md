---
name: crawfish-authoring-mcp-py
description: >
  Author mcp/*.py — MCPConnection instances; auth is a secret reference (env-var name),
  never inline. Load when adding an external tool server. A new connection re-enters the
  consent gate (craw code grant).
user-invocable: false
allowed-tools: Read, Grep
---

# Authoring `mcp/*.py`

Derived from `docs/specs/craw-code/authoring/mcp-py.md`. Golden:
`demo/craw-code-golden/mcp/github.py`.

Each `mcp/*.py` holds one or more **module-level `MCPConnection` instances**. The compiler
discovers them into `DefinitionAssets.mcp`; the connection's `tools` list joins the per-agent
tool allowlist.

```python
"""GitHub MCP connection. auth is a SECRET REFERENCE — an env-var name, never a value."""
from __future__ import annotations
from crawfish.definition.types import MCPConnection

github = MCPConnection(
    name="github",
    description="GitHub issues/PRs server.",
    command=["npx", "-y", "@modelcontextprotocol/server-github"],
    auth="GITHUB_TOKEN",          # ← reference only; resolved into the server env at run time
    tools=["create_issue", "search_issues"],
)
```

## auth is always a reference

The credential resolves at run time into the server env and never reaches a prompt, a
`config`, or a log. Writing a literal token here is the wrong shape; the secret-shaped lint
(CRA-276) fails it closed, and the scrub layer would redact it anyway.

> **Spine rule (secrets-by-reference):** A secret is an env-var name (auth="GITHUB_TOKEN"),
> never an inline value.

## A new connection re-enters the consent gate

Adding an `MCPConnection` adds a **capability** — egress plus a secret reference — that
bypasses the install-time consent gate. So `craw code sync`/`new` calls `regate_generated`,
which diffs the newly-declared capabilities against the prior `Grant`. In the unattended
agent loop the default is deny — an un-consented new MCP raises `ConsentRequired` and stops
the run (`retryable:false`). The consent surface shows the secret by reference name only.

> **Spine rule (consent-regate):** A new MCP/dependency re-enters the consent gate (craw
> code grant, CRA-277).

List the exact `tools` the connection exposes so the per-agent allowlist stays checkable —
never bind an agent to a tool the connection does not declare.
