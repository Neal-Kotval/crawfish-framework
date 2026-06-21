"""MCP wiring for runtimes.

Turns a Definition's ``assets.mcp`` connections into the config a backend needs, and
gates them by the agent's tool allowlist. Credentials are resolved **by reference**
(an env-var name) and injected into the server env — never into the prompt.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from crawfish.definition.types import AgentSpec, Definition, MCPConnection

__all__ = ["build_mcp_config", "allowed_mcp_tools", "resolve_secret"]


def resolve_secret(ref: str | None, env: Mapping[str, str] | None = None) -> str | None:
    """Resolve a secret reference (env-var name) to its value, or None if unset."""
    if not ref:
        return None
    return (env or os.environ).get(ref)


def build_mcp_config(
    connections: list[MCPConnection], env: Mapping[str, str] | None = None
) -> dict[str, object]:
    """Build a ``{"mcpServers": {...}}`` config (the shape `claude --mcp-config` reads).

    Credentials are referenced **by name only** — the secret VALUE is **never** written
    into this config, which is passed to ``claude`` as an arg the agent can read.

    CRA-178 (closes the MCP secret-leak): an auth-bearing connection contributes only
    ``auth_ref`` + ``brokered: true`` (the env-var *name*, never its value). The value is
    delivered out-of-band by the :class:`~crawfish.secrets.SecretBroker`
    (:func:`crawfish.secrets.brokered_mcp_config`, Grant-gated + STATIC-only + audited) —
    a prompt-injected agent cannot exfiltrate a value that was never placed in its
    process tree. The ``env`` argument is accepted for back-compat but intentionally
    unused: this path no longer resolves secrets to values.
    """
    _ = env  # back-compat only; secret values are never resolved into the config (CRA-178)
    servers: dict[str, object] = {}
    for conn in connections:
        server: dict[str, object] = {}
        if conn.command:
            server["command"] = conn.command[0]
            server["args"] = conn.command[1:]
        if conn.url:
            server["url"] = conn.url
        if conn.auth:
            # reference only — the broker injects the value at egress, never here.
            server["auth_ref"] = conn.auth
            server["brokered"] = True
        servers[conn.name] = server
    return {"mcpServers": servers}


def allowed_mcp_tools(definition: Definition, agent: AgentSpec) -> list[str]:
    """The MCP tool names this agent may call (intersection of its allowlist and the
    tools the connected servers expose)."""
    exposed = {t for conn in definition.assets.mcp for t in conn.tools}
    return [t for t in agent.tools if t in exposed]
