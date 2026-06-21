"""CRA-116 acceptance: MCP connections compile, expose tools, gate by allowlist."""

from __future__ import annotations

import shutil
from pathlib import Path

from crawfish.core.context import RunContext
from crawfish.definition import Definition, MCPConnection
from crawfish.runtime import (
    CommandRuntime,
    RunRequest,
    allowed_mcp_tools,
    build_mcp_config,
    compile_prompt,
    pick_agent,
)
from crawfish.store import SqliteStore

FIXTURES = Path(__file__).parent / "fixtures"


def _definition(tmp_path: Path) -> Definition:
    dest = tmp_path / "full"
    shutil.copytree(FIXTURES / "full", dest)
    return Definition.from_package(str(dest))


def test_mcp_connection_compiles(tmp_path: Path) -> None:
    d = _definition(tmp_path)
    conn = next(c for c in d.assets.mcp if c.name == "linear")
    assert conn.tools == ["linear_create_issue", "linear_search"]
    assert conn.auth == "LINEAR_API_KEY"


def test_mcp_tools_available_to_unrestricted_agent(tmp_path: Path) -> None:
    d = _definition(tmp_path)
    reviewer = d.agent("reviewer")  # no explicit tools -> gets all available
    assert "linear_create_issue" in reviewer.tools
    assert "open_pr" in reviewer.tools


def test_allowlist_respected(tmp_path: Path) -> None:
    d = _definition(tmp_path)
    # scout explicitly binds only open_pr -> no MCP tools
    assert allowed_mcp_tools(d, d.agent("scout")) == []
    # reviewer unrestricted -> both linear tools
    assert set(allowed_mcp_tools(d, d.agent("reviewer"))) == {
        "linear_create_issue",
        "linear_search",
    }


def test_build_mcp_config_references_secret_never_embeds_value() -> None:
    # CRA-178: the secret VALUE must never land in the agent-readable config; only the
    # reference name + a brokered marker do.
    import json

    conn = MCPConnection(name="linear", command=["x"], auth="LINEAR_API_KEY", tools=["t"])
    config = build_mcp_config([conn], env={"LINEAR_API_KEY": "secret-value"})
    server = config["mcpServers"]["linear"]  # type: ignore[index]
    assert "env" not in server  # no value-bearing env
    assert server["auth_ref"] == "LINEAR_API_KEY"  # reference only
    assert server["brokered"] is True
    assert "secret-value" not in json.dumps(config)  # the value appears nowhere


def test_secret_never_in_prompt(tmp_path: Path) -> None:
    d = _definition(tmp_path)
    agent = pick_agent(d, "reviewer")
    prompt = compile_prompt(d, agent, {"repo": "acme/app", "pr_body": "x"})
    assert "LINEAR_API_KEY" not in prompt  # the reference name isn't leaked either


async def test_command_runtime_passes_mcp_config(tmp_path: Path) -> None:
    d = _definition(tmp_path)
    seen: dict[str, list[str]] = {}

    async def fake_transport(args: list[str], prompt: str) -> str:
        seen["args"] = args
        assert "secret-value" not in prompt  # secret not in prompt
        return '{"type":"result","total_cost_usd":0,"result":"ok","session_id":"s"}'

    rt = CommandRuntime(transport=fake_transport)
    ctx = RunContext(store=SqliteStore())
    await rt.run(RunRequest(definition=d, role="reviewer"), ctx)
    assert "--mcp-config" in seen["args"]
    assert "--allowedTools" in seen["args"]
    allowed = seen["args"][seen["args"].index("--allowedTools") + 1]
    assert "linear_create_issue" in allowed
