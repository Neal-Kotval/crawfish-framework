"""CommandRuntime — the zero-key reference backend via ``claude -p``.

``pip install crawfish`` + ``claude -p`` runs a pipeline with nothing hosted. The
subprocess call is injected (the ``transport``) so tests feed canned ``stream-json``
and run **deterministically with no live model calls** — the same seam record/replay
(:mod:`crawfish.runtime.replay`) builds on.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable

from crawfish.core.context import RunContext
from crawfish.provider import ModelsConfig, resolve_model
from crawfish.runtime.base import (
    AgentRuntime,
    EventKind,
    RunRequest,
    RunResult,
    RuntimeEvent,
    ToolCall,
)
from crawfish.runtime.mcp import allowed_mcp_tools, build_mcp_config
from crawfish.runtime.prompt import compile_prompt, pick_agent

__all__ = ["CommandRuntime", "Transport"]

# Given (args, prompt), return the process's raw stdout (stream-json, one JSON/line).
Transport = Callable[[list[str], str], Awaitable[str]]

DEFAULT_MODEL = "claude-opus-4-8"  # Claude-first default for unpinned agents (ADR 0005)


def _default_transport(claude_bin: str) -> Transport:
    async def spawn(args: list[str], prompt: str) -> str:
        proc = await asyncio.create_subprocess_exec(
            claude_bin,
            "-p",
            prompt,
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(
                f"`{claude_bin} -p` exited {proc.returncode}: {stderr.decode()[:500]}"
            )
        return stdout.decode()

    return spawn


class CommandRuntime(AgentRuntime):
    name = "command"

    def __init__(
        self,
        *,
        claude_bin: str = "claude",
        transport: Transport | None = None,
        default_model: str = DEFAULT_MODEL,
        config: ModelsConfig | None = None,
        permission_mode: str | None = None,
    ) -> None:
        self._transport = transport or _default_transport(claude_bin)
        self._default_model = default_model
        self._config = config
        self._permission_mode = permission_mode

    def _resolve_model(self, request: RunRequest) -> str:
        if request.model:
            return request.model
        agent = pick_agent(request.definition, request.role)
        # Delegate field->id resolution to the single shared resolver (ADR 0013).
        # The project ModelsConfig supplies named aliases + the configured default;
        # an unconfigured project falls back to the Claude-first DEFAULT_MODEL (ADR 0005).
        return resolve_model(agent.model, default=self._default_model, config=self._config)

    def _build_args(self, request: RunRequest, model: str) -> list[str]:
        args = ["--output-format", "stream-json", "--verbose", "--model", model]
        if self._permission_mode:
            args += ["--permission-mode", self._permission_mode]
        if request.session_id:
            args += ["--resume", request.session_id]
        return args

    async def run(self, request: RunRequest, ctx: RunContext) -> RunResult:
        ctx.cancel_token.raise_if_cancelled()
        agent = pick_agent(request.definition, request.role)
        model = self._resolve_model(request)
        prompt = compile_prompt(request.definition, agent, request.inputs)

        args = self._build_args(request, model)
        if request.definition.assets.mcp:
            # Expose connected MCP servers' tools; gate by the agent's allowlist.
            # Secrets are injected into the server env by reference, never the prompt.
            config = build_mcp_config(request.definition.assets.mcp)
            args += ["--mcp-config", json.dumps(config)]
            allowed = allowed_mcp_tools(request.definition, agent)
            if allowed:
                args += ["--allowedTools", ",".join(allowed)]

        stdout = await self._transport(args, prompt)
        result = _parse_stream_json(stdout, model)

        ctx.cost_budget.charge(result.cost_usd)
        self._emit_telemetry(ctx, result, self.name)
        return result


def _parse_stream_json(stdout: str, model: str) -> RunResult:
    """Parse ``--output-format stream-json`` (one JSON object per line)."""
    events: list[RuntimeEvent] = []
    text = ""
    cost = 0.0
    session_id: str | None = None

    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        typ = obj.get("type")
        session_id = obj.get("session_id", session_id)

        if typ == "assistant":
            for block in obj.get("message", {}).get("content", []):
                if block.get("type") == "text":
                    events.append(
                        RuntimeEvent(kind=EventKind.TEXT, text=block["text"], session_id=session_id)
                    )
                elif block.get("type") == "tool_use":
                    events.append(
                        RuntimeEvent(
                            kind=EventKind.TOOL_USE,
                            tool=ToolCall(
                                id=block.get("id", ""),
                                name=block.get("name", ""),
                                input=block.get("input", {}),
                            ),
                            session_id=session_id,
                        )
                    )
        elif typ == "user":
            for block in obj.get("message", {}).get("content", []):
                if block.get("type") == "tool_result":
                    content = block.get("content", "")
                    if isinstance(content, list):
                        content = "".join(c.get("text", "") for c in content if isinstance(c, dict))
                    events.append(
                        RuntimeEvent(
                            kind=EventKind.TOOL_RESULT, text=str(content), session_id=session_id
                        )
                    )
        elif typ == "result":
            cost = float(obj.get("total_cost_usd", 0.0) or 0.0)
            text = str(obj.get("result", ""))
            events.append(
                RuntimeEvent(kind=EventKind.RESULT, text=text, cost_usd=cost, session_id=session_id)
            )

    if not text:  # fall back to concatenated assistant text if no result line
        text = "".join(e.text for e in events if e.kind is EventKind.TEXT)
    return RunResult(text=text, session_id=session_id, cost_usd=cost, model=model, events=events)
