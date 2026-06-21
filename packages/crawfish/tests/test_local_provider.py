"""CRA-182 / ADR 0011 acceptance — LocalHTTPProvider.

The cheap local-inference leg: a seeded OpenAI-compatible HTTP client behind the frozen
``Provider`` protocol. In tests the transport is a FAKE — NO real HTTP, no egress, no
credential. ``model="local"`` routes to it through ``ProviderRuntime``.
"""

from __future__ import annotations

import json

import pytest

from crawfish.core.context import RunContext
from crawfish.definition.types import AgentSpec, Definition, TeamSpec
from crawfish.provider import Provider
from crawfish.routing import RoutingPolicy, RoutingRule
from crawfish.runtime import (
    LocalHTTPProvider,
    OpenAIChatRequest,
    ProviderRuntime,
    RoutingRuntime,
    RunRequest,
)
from crawfish.runtime.local_provider import _parse_chat_completion
from crawfish.store import SqliteStore


def _ctx() -> RunContext:
    return RunContext(store=SqliteStore())


def _definition() -> Definition:
    return Definition(team=TeamSpec(agents=[AgentSpec(role="scout", prompt="scan")]))


def _fake_transport(captured: list[OpenAIChatRequest]):
    """A fake local-server transport: records the request, returns canned JSON. No HTTP."""

    async def transport(req: OpenAIChatRequest) -> str:
        captured.append(req)
        return json.dumps(
            {"choices": [{"message": {"role": "assistant", "content": "local says hi"}}]}
        )

    return transport


# --- structural conformance + credential-free, seeded, no-egress ----------------------


def test_local_provider_is_a_provider() -> None:
    assert isinstance(LocalHTTPProvider(transport=_fake_transport([])), Provider)
    assert LocalHTTPProvider().name == "local"


async def test_local_provider_runs_via_mock_transport_no_http() -> None:
    captured: list[OpenAIChatRequest] = []
    provider = LocalHTTPProvider(models=["local"], transport=_fake_transport(captured), seed=42)
    result = await provider.run(RunRequest(definition=_definition(), role="scout"), _ctx())

    assert result.text == "local says hi"
    assert result.model == "local"
    assert result.cost_usd == 0.0  # local inference burns no metered budget
    # The request carried the pinned seed and the OpenAI-compatible body shape.
    assert len(captured) == 1
    body = captured[0].as_body()
    assert body["seed"] == 42
    assert body["messages"][0]["role"] == "user"


async def test_local_provider_without_transport_raises_not_egress() -> None:
    # No transport injected -> NotImplementedError, never a silent network call.
    provider = LocalHTTPProvider(models=["local"])
    with pytest.raises(NotImplementedError):
        await provider.run(RunRequest(definition=_definition(), role="scout"), _ctx())


# --- model="local" routes to the local provider via ProviderRuntime -------------------


async def test_model_local_routes_to_local_provider() -> None:
    captured: list[OpenAIChatRequest] = []
    local = LocalHTTPProvider(models=["local"], transport=_fake_transport(captured))
    rt = ProviderRuntime([local], default_model="local")
    # An agent pinned to "local" resolves to the LocalHTTPProvider.
    d = Definition(team=TeamSpec(agents=[AgentSpec(role="scout", prompt="s", model="local")]))
    result = await rt.run(RunRequest(definition=d, role="scout"), _ctx())
    assert result.model == "local" and result.text == "local says hi"
    assert captured  # the local transport was the one that answered


async def test_routing_policy_sends_cheap_step_to_local() -> None:
    captured: list[OpenAIChatRequest] = []
    local = LocalHTTPProvider(models=["local"], transport=_fake_transport(captured))
    policy = RoutingPolicy(rules=(RoutingRule(role="scout", model="local"),))
    rt = RoutingRuntime(
        ProviderRuntime([local], default_model="local"), policy, default_model="claude-opus-4-8"
    )
    d = _definition()
    result = await rt.run(RunRequest(definition=d, role="scout"), _ctx())
    assert result.model == "local"
    assert captured  # routed cheap step landed on the local provider


# --- response parsing tolerates shape drift -------------------------------------------


def test_parse_chat_completion_shapes() -> None:
    assert _parse_chat_completion('{"choices":[{"message":{"content":"hi"}}]}') == "hi"
    assert _parse_chat_completion('{"choices":[{"text":"old"}]}') == "old"  # legacy shape
    assert _parse_chat_completion("not json") == ""
    assert _parse_chat_completion('{"choices":[]}') == ""
