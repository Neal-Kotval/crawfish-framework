"""Smart model routing — send cheap steps to cheap/local models, hard steps to strong ones.

CRA-182's cost lever #1. A :class:`RoutingPolicy` is a typed, frozen, ordered list of
:class:`RoutingRule` s. Each rule *matches* an agent (by role, by a coarse cost
**tier**, or unconditionally) and, on the first match, names the model field that agent
should run — e.g. route a low-stakes ``"scout"`` step to ``"local"`` and an expensive
``"reviewer"`` step to the strong model. A rule's chosen field is **never** a final
model id on its own: it is handed to the *single shared resolver*
(:func:`crawfish.provider.resolve_model`) exactly as the runtime does, so aliases and the
configured default expand identically and the cost preview can't drift from the run.

The drift guarantee (verified by CRA-186): both the runtime path
(:class:`~crawfish.runtime.routing_runtime.RoutingRuntime`) and the dry-run preview
(:func:`crawfish.cost.estimate_cost`) call :func:`route_model` here, which calls
``resolve_model`` — there is no second resolution path.

Routing is a pure function of the (definition, role, policy, config) tuple: deterministic,
no I/O, no model call. A routing decision can be surfaced as a typed
:class:`~crawfish.emission.Emission` (``MODEL`` kind, ``attrs["routed_by"]``) via
:func:`routing_emission` so the dashboard/anomaly engine can see why a model was chosen.
"""

from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from crawfish.provider import ModelsConfig, resolve_model

if TYPE_CHECKING:
    from crawfish.definition.types import AgentSpec, Definition
    from crawfish.emission import Emission

__all__ = [
    "CostTier",
    "RoutingRule",
    "RoutingPolicy",
    "RoutingDecision",
    "agent_tier",
    "route_model",
    "route_decision",
    "routing_emission",
]


class CostTier(str, Enum):
    """A coarse stakes/complexity classification for a step.

    The tier is *advisory* metadata an author may pin on a rule's match side; it does
    not itself pick a model. ``CHEAP`` steps are low-stakes/simple (route to a cheap or
    ``local`` model); ``STRONG`` steps are high-stakes/hard (route to the strong model);
    ``STANDARD`` is the unclassified middle. ``(str, Enum)`` per ADR 0004.
    """

    CHEAP = "cheap"
    STANDARD = "standard"
    STRONG = "strong"


def agent_tier(agent: AgentSpec) -> CostTier | None:
    """Read a coarse :class:`CostTier` an author declared on an agent, if any.

    The tier is read from the agent's ``policies`` list (a stringly-typed authoring
    surface that already exists on :class:`AgentSpec`), matching ``"tier:cheap"`` /
    ``"tier:standard"`` / ``"tier:strong"``. No tier declared returns ``None`` (the rule's
    ``tier`` condition then only matches a rule whose ``tier`` is also ``None``). Pure.
    """
    for policy in agent.policies:
        if policy.startswith("tier:"):
            value = policy.split(":", 1)[1]
            try:
                return CostTier(value)
            except ValueError:
                continue
    return None


class RoutingRule(BaseModel):
    """One match→model rule. Frozen.

    Match side (all conditions that are set must hold; unset conditions match anything):

    * ``role`` — exact agent role to match (``None`` matches any role).
    * ``tier`` — match agents whose *declared* tier equals this (see
      :func:`agent_tier`); ``None`` matches any tier.

    Target side:

    * ``model`` — the model **field** to route matched agents to. It is resolved through
      the shared :func:`resolve_model` (so ``"local"``, a configured alias, or a concrete
      id all work). A list expresses a failover order, resolved to its primary for the
      cost preview (the runtime keeps the whole list for failover).
    """

    model_config = {"frozen": True}

    role: str | None = None
    tier: CostTier | None = None
    model: str | list[str]

    def matches(self, agent: AgentSpec) -> bool:
        """True if this rule applies to ``agent``."""
        if self.role is not None and agent.role != self.role:
            return False
        if self.tier is not None and agent_tier(agent) is not self.tier:
            return False
        return True


class RoutingPolicy(BaseModel):
    """An ordered list of :class:`RoutingRule` s; first match wins. Frozen.

    :meth:`select_field` returns the model *field* the first matching rule names, or
    ``None`` when no rule matches (the agent's own ``model`` field is then left intact —
    routing is purely additive and never silently strips an explicit pin). Resolution to
    a concrete id is **always** done by :func:`route_model` via the shared resolver.
    """

    model_config = {"frozen": True}

    rules: tuple[RoutingRule, ...] = Field(default_factory=tuple)

    def select_field(self, agent: AgentSpec) -> str | list[str] | None:
        """The model field the first matching rule routes ``agent`` to, or ``None``."""
        for rule in self.rules:
            if rule.matches(agent):
                return rule.model
        return None


class RoutingDecision(BaseModel):
    """The deterministic outcome of routing one agent. Frozen.

    ``resolved`` is the concrete model id (post shared-resolver). ``routed`` is True when
    a rule fired (vs. falling back to the agent's own field). ``source`` records *why*:
    ``"rule"`` (a policy rule matched), ``"agent"`` (no rule; the agent's own field used),
    or ``"default"`` (no rule and an unpinned agent).
    """

    model_config = {"frozen": True}

    role: str
    resolved: str
    routed: bool
    source: str


def _selected_field(
    agent: AgentSpec, policy: RoutingPolicy | None
) -> tuple[str | list[str] | None, bool]:
    """(field, routed) — the routed field if a rule fired, else the agent's own field."""
    if policy is not None:
        chosen = policy.select_field(agent)
        if chosen is not None:
            return chosen, True
    return agent.model, False


def route_decision(
    definition: Definition,
    role: str | None = None,
    *,
    policy: RoutingPolicy | None = None,
    default: str,
    config: ModelsConfig | None = None,
) -> RoutingDecision:
    """Resolve one agent's model through ``policy`` then the **shared** resolver.

    The single decision point CRA-182 routes everything through. A matching rule's field
    (or, absent a match, the agent's own ``model``) is expanded by
    :func:`crawfish.provider.resolve_model` with the same ``default``/``config`` the
    runtime uses — so the runtime and :func:`crawfish.cost.estimate_cost` can never
    disagree (CRA-186). Deterministic; no I/O.
    """
    # Local import avoids a module-load cycle (definition never imports routing).
    from crawfish.runtime.prompt import pick_agent

    agent = pick_agent(definition, role)
    field, routed = _selected_field(agent, policy)
    resolved = resolve_model(field, default=default, config=config)
    source = "rule" if routed else ("agent" if agent.model is not None else "default")
    return RoutingDecision(role=agent.role, resolved=resolved, routed=routed, source=source)


def route_model(
    definition: Definition,
    role: str | None = None,
    *,
    policy: RoutingPolicy | None = None,
    default: str,
    config: ModelsConfig | None = None,
) -> str:
    """The concrete model id for one agent after routing. Thin wrapper over
    :func:`route_decision` returning just the resolved id."""
    return route_decision(definition, role, policy=policy, default=default, config=config).resolved


def routing_emission(decision: RoutingDecision, *, run_id: str, org_id: str = "local") -> Emission:
    """A typed ``MODEL`` :class:`Emission` recording a routing decision (no cost yet).

    Lets the dashboard/anomaly engine see *why* a model was picked. ``cost_usd`` is 0.0
    (the spend is charged later by the runtime when the model actually answers); the
    routing metadata lives under ``attrs``. Not tainted — a routing choice derives from
    static config, never fluid input.
    """
    from crawfish.emission import Emission, EmissionKind

    return Emission(
        kind=EmissionKind.MODEL,
        run_id=run_id,
        org_id=org_id,
        node_id=decision.role,
        attrs={
            "model": decision.resolved,
            "cost_usd": 0.0,
            "routed_by": decision.source,
            "routed": decision.routed,
        },
    )
