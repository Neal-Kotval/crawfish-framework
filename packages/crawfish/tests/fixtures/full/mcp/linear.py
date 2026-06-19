"""A reference MCP connection (Linear). Auth is by reference (an env-var name)."""

from __future__ import annotations

from crawfish.definition import MCPConnection

linear = MCPConnection(
    name="linear",
    description="Linear issue tracker via MCP",
    command=["npx", "-y", "@linear/mcp-server"],
    auth="LINEAR_API_KEY",  # secret reference — resolved at run time, never inline
    tools=["linear_create_issue", "linear_search"],
)
