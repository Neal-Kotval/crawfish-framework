"""CRA-182 acceptance — smart model routing.

A :class:`RoutingPolicy` sends a cheap/low-stakes step to a cheap (or ``local``) model
and an expensive step to the strong model — deterministically, through the SINGLE shared
``resolve_model`` (via ``route_decision``). The runtime path (``RoutingRuntime``) and the
cost preview (``estimate_cost``) resolve to the SAME model id — no drift (CRA-186).

Fully deterministic: no live model calls, no egress. A ``MockProvider`` answers turns.
"""

from __future__ import annotations

import pytest

from crawfish.core.context import RunContext
from crawfish.cost import DEFAULT_MODEL_PRICES, estimate_cost
from crawfish.definition.types import AgentSpec, Coordination, Definition, TeamSpec
from crawfish.emission import EmissionKind, read_emissions
from crawfish.provider import ModelsConfig
from crawfish.routing import (
    CostTier,
    RoutingPolicy,
    RoutingRule,
    agent_tier,
    route_decision,
    route_model,
)
from crawfish.runtime import MockProvider, ProviderRuntime, RoutingRuntime, RunRequest
from crawfish.store import SqliteStore

STRONG = "claude-opus-4-8"
CHEAP = "claude-haiku-4-5"


def _two_step_team() -> Definition:
    """A cheap scout step (tier:cheap) + an expensive reviewer step (tier:strong)."""
    return Definition(
        team=TeamSpec(
            agents=[
                AgentSpec(role="scout", prompt="scan", policies=["tier:cheap"]),
                AgentSpec(role="reviewer", prompt="judge", policies=["tier:strong"]),
            ],
            coordination=Coordination.SINGLE,
        )
    )


def _policy() -> RoutingPolicy:
    # First match wins: cheap tier -> local/cheap; strong tier -> the strong model.
    return RoutingPolicy(
        rules=(
            RoutingRule(tier=CostTier.CHEAP, model=CHEAP),
            RoutingRule(tier=CostTier.STRONG, model=STRONG),
        )
    )


def _ctx() -> RunContext:
    return RunContext(store=SqliteStore())


# --- pure routing: cheap step -> cheap model, expensive step -> strong model ----------


def test_routing_sends_cheap_to_cheap_and_strong_to_strong() -> None:
    d, policy = _two_step_team(), _policy()
    assert route_model(d, "scout", policy=policy, default=STRONG) == CHEAP
    assert route_model(d, "reviewer", policy=policy, default=STRONG) == STRONG


def test_routing_by_role_rule() -> None:
    d = _two_step_team()
    policy = RoutingPolicy(rules=(RoutingRule(role="scout", model="local"),))
    assert route_model(d, "scout", policy=policy, default=STRONG) == "local"
    # No rule matches reviewer -> falls back to the agent's own field (unpinned -> default)
    dec = route_decision(d, "reviewer", policy=policy, default=STRONG)
    assert dec.resolved == STRONG and dec.routed is False and dec.source == "default"


def test_routing_uses_shared_resolver_for_aliases() -> None:
    # A routed field that is an alias is expanded by the SHARED resolve_model.
    d = _two_step_team()
    config = ModelsConfig(aliases={"fast": CHEAP})
    policy = RoutingPolicy(rules=(RoutingRule(tier=CostTier.CHEAP, model="fast"),))
    assert route_model(d, "scout", policy=policy, default=STRONG, config=config) == CHEAP


def test_routing_does_not_strip_explicit_pin_when_no_rule_matches() -> None:
    d = Definition(team=TeamSpec(agents=[AgentSpec(role="a", model="pinned-id")]))
    dec = route_decision(d, "a", policy=RoutingPolicy(), default=STRONG)
    assert dec.resolved == "pinned-id" and dec.source == "agent"


def test_agent_tier_reads_policy_marker() -> None:
    assert agent_tier(AgentSpec(role="a", policies=["tier:cheap"])) is CostTier.CHEAP
    assert agent_tier(AgentSpec(role="a")) is None


# --- no drift: estimate_cost matches the routed runtime model -------------------------


async def test_estimate_cost_matches_routed_runtime_model() -> None:
    d, policy = _two_step_team(), _policy()

    # Preview under the SAME policy: cheap step priced at CHEAP, strong at STRONG.
    est = estimate_cost(d, items=1, routing=policy)
    assert est.per_model == {
        CHEAP: pytest.approx(DEFAULT_MODEL_PRICES[CHEAP]),
        STRONG: pytest.approx(DEFAULT_MODEL_PRICES[STRONG]),
    }

    # The runtime routes each step to the exact same id the preview priced (no drift).
    provider = MockProvider("local-or-strong", [CHEAP, STRONG])
    rt = RoutingRuntime(
        ProviderRuntime([provider], default_model=STRONG),
        policy,
        default_model=STRONG,
    )
    scout = await rt.run(RunRequest(definition=d, role="scout"), _ctx())
    reviewer = await rt.run(RunRequest(definition=d, role="reviewer"), _ctx())
    assert scout.model == CHEAP  # cheap step ran on the cheap model the preview priced
    assert reviewer.model == STRONG  # strong step ran on the strong model


async def test_routing_runtime_pins_local_model() -> None:
    d = _two_step_team()
    policy = RoutingPolicy(rules=(RoutingRule(tier=CostTier.CHEAP, model="local"),))
    provider = MockProvider("local", ["local"])
    rt = RoutingRuntime(
        ProviderRuntime([provider], default_model=STRONG), policy, default_model=STRONG
    )
    result = await rt.run(RunRequest(definition=d, role="scout"), _ctx())
    assert result.model == "local"


async def test_explicit_per_run_model_bypasses_routing() -> None:
    d = _two_step_team()
    provider = MockProvider("p", ["override-id", CHEAP])
    rt = RoutingRuntime(
        ProviderRuntime([provider], default_model=STRONG), _policy(), default_model=STRONG
    )
    # An explicit per-run pin wins over the policy (which would have chosen CHEAP).
    result = await rt.run(RunRequest(definition=d, role="scout", model="override-id"), _ctx())
    assert result.model == "override-id"


async def test_routing_decision_emitted_when_requested() -> None:
    d = _two_step_team()
    provider = MockProvider("p", [CHEAP])
    rt = RoutingRuntime(
        ProviderRuntime([provider], default_model=STRONG),
        _policy(),
        default_model=STRONG,
        emit_decision=True,
    )
    ctx = _ctx()
    await rt.run(RunRequest(definition=d, role="scout"), ctx)
    ems = read_emissions(ctx.store, ctx.run_id)
    routed = [em for em in ems if em.kind is EmissionKind.MODEL and em.attrs.get("routed") is True]
    assert routed and routed[0].attrs["model"] == CHEAP
    assert routed[0].attrs["routed_by"] == "rule"
