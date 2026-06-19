"""MCP wiring for runtimes (CRA-116).

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

    Secret values land in each server's ``env`` (by reference), never in any prompt.
    """
    servers: dict[str, object] = {}
    for conn in connections:
        server: dict[str, object] = {}
        if conn.command:
            server["command"] = conn.command[0]
            server["args"] = conn.command[1:]
        if conn.url:
            server["url"] = conn.url
        secret = resolve_secret(conn.auth, env)
        if secret is not None:
            # injected into the server's environment, keyed by the reference name
            server["env"] = {conn.auth: secret}
        servers[conn.name] = server
    return {"mcpServers": servers}


def allowed_mcp_tools(definition: Definition, agent: AgentSpec) -> list[str]:
    """The MCP tool names this agent may call (intersection of its allowlist and the
    tools the connected servers expose)."""
    exposed = {t for conn in definition.assets.mcp for t in conn.tools}
    return [t for t in agent.tools if t in exposed]
