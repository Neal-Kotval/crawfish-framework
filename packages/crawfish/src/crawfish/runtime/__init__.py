"""AgentRuntime backends — the swappable agent loop."""

from __future__ import annotations

from crawfish.runtime.base import (
    AgentRuntime,
    EventKind,
    RunRequest,
    RunResult,
    RuntimeEvent,
    ToolCall,
)
from crawfish.runtime.command import CommandRuntime, Transport
from crawfish.runtime.context_strategy import (
    CompactionResult,
    ContextStrategy,
    ConversationTurn,
    ExponentialCompact,
    LinearCompact,
    MaxTokens,
    Summarize,
    estimate_tokens,
    manage_context,
    resolve_strategy,
)
from crawfish.runtime.local_provider import (
    LocalHTTPProvider,
    LocalTransport,
    OpenAIChatRequest,
)
from crawfish.runtime.mcp import allowed_mcp_tools, build_mcp_config, resolve_secret
from crawfish.runtime.mock import MockRuntime
from crawfish.runtime.prompt import compile_prompt, pick_agent, split_inputs
from crawfish.runtime.provider_runtime import (
    ProviderFailover,
    ProviderRuntime,
    expand_candidates,
)
from crawfish.runtime.providers import ClientProvider, MockProvider
from crawfish.runtime.replay import CassetteMiss, RecordReplayRuntime
from crawfish.runtime.routing_runtime import RoutingRuntime
from crawfish.runtime.select import RUNTIME_FACTORIES, get_runtime
from crawfish.runtime.stubs import ClientRuntime, ManagedRuntime
from crawfish.runtime.team import run_team

__all__ = [
    "AgentRuntime",
    "run_team",
    "EventKind",
    "RuntimeEvent",
    "ToolCall",
    "RunRequest",
    "RunResult",
    "CommandRuntime",
    "Transport",
    "MockRuntime",
    "ClientRuntime",
    "ManagedRuntime",
    "ProviderRuntime",
    "ProviderFailover",
    "expand_candidates",
    "MockProvider",
    "ClientProvider",
    "LocalHTTPProvider",
    "LocalTransport",
    "OpenAIChatRequest",
    "RoutingRuntime",
    "RecordReplayRuntime",
    "CassetteMiss",
    "get_runtime",
    "RUNTIME_FACTORIES",
    "compile_prompt",
    "pick_agent",
    "split_inputs",
    # context strategies
    "ContextStrategy",
    "ConversationTurn",
    "CompactionResult",
    "MaxTokens",
    "LinearCompact",
    "ExponentialCompact",
    "Summarize",
    "estimate_tokens",
    "resolve_strategy",
    "manage_context",
    # mcp
    "build_mcp_config",
    "allowed_mcp_tools",
    "resolve_secret",
]
