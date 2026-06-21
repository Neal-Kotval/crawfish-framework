"""Team coordination — executing a TeamSpec topology.

Decision (locked): lean on Claude's **hierarchical subagent model**, not a bespoke
peer-to-peer message bus. Communication is **delegation-in / typed-result-out** — a
lead dispatches subagents and combines their typed results; there is no free-form
channel, which preserves typing and the prompt-injection boundary (a subagent's result
re-enters the lead as fluid data, never as instructions).

The coordinator is runtime-agnostic (works with any :class:`AgentRuntime`, incl. the
mock — so tests are deterministic). For backends with native hierarchical subagents
(CommandRuntime/CMA) this same topology can later collapse into one native multiagent
call; the explicit coordinator is the portable default (ADR 0007).

Cross-agent state is a first-class, typed, taint-aware :class:`Context` artifact (not a
raw ``{role}_result`` string). Each agent's typed result is carried forward as a
:class:`~crawfish.runtime.context_artifact.ContextEntry` preserving its value, taint and
lineage; a :class:`~crawfish.runtime.context_strategy.ContextCarryStrategy` chooses what
subset is forwarded. Tainted (fluid-derived) entries reach the next agent as **data**
inside the fluid block, never as instructions — the static/fluid boundary holds.
"""

from __future__ import annotations

from crawfish.core.context import RunContext
from crawfish.core.types import JSONValue
from crawfish.definition.types import Coordination, Definition
from crawfish.output import Output
from crawfish.runtime.base import AgentRuntime, RunRequest, RunResult
from crawfish.runtime.context_artifact import Context
from crawfish.runtime.context_strategy import ContextCarryStrategy, resolve_carry_strategy
from crawfish.runtime.prompt import pick_agent, split_inputs

__all__ = ["run_team"]


def _result_output(
    definition: Definition,
    role: str,
    inputs: dict[str, JSONValue],
    result: RunResult,
    *,
    upstream_tainted: bool,
) -> Output[JSONValue]:
    """Wrap a RunResult's text as a typed Output, carrying taint forward.

    A result is tainted if the agent consumed any **fluid** (untrusted) input, or if the
    upstream context it built on was already tainted — fluid-derived state stays tainted
    as it crosses into the next agent (it re-enters as DATA, never instructions).
    """
    _static, fluid = split_inputs(definition, inputs)
    tainted = upstream_tainted or bool(fluid)
    return Output(value=result.text, produced_by=role, tainted=tainted)


def _carry_for(definition: Definition) -> ContextCarryStrategy:
    """Resolve the team's declared context-carry strategy (deterministic, model-free)."""
    return resolve_carry_strategy(definition.team.context_carry)


async def run_team(
    definition: Definition,
    inputs: dict[str, JSONValue],
    ctx: RunContext,
    runtime: AgentRuntime,
) -> RunResult:
    """Execute a Definition's team per its coordination topology, return one result."""
    topology = definition.team.coordination
    if topology is Coordination.SEQUENTIAL:
        return await _run_sequential(definition, inputs, ctx, runtime)
    if topology is Coordination.LEAD:
        return await _run_lead(definition, inputs, ctx, runtime)
    return await _run_single(definition, inputs, ctx, runtime)


async def _run_single(
    definition: Definition,
    inputs: dict[str, JSONValue],
    ctx: RunContext,
    runtime: AgentRuntime,
) -> RunResult:
    agent = pick_agent(definition, None)
    return await runtime.run(RunRequest(definition=definition, role=agent.role, inputs=inputs), ctx)


async def _run_sequential(
    definition: Definition,
    inputs: dict[str, JSONValue],
    ctx: RunContext,
    runtime: AgentRuntime,
) -> RunResult:
    """Agents run in declared order; each typed result threads into the next via Context.

    Cross-agent state is a typed, taint-aware :class:`Context` (not a raw string): each
    agent's result is carried forward as a :class:`ContextEntry` (value + taint +
    lineage), reduced by the team's carry strategy, and rendered as the next agent's
    inputs. The carried entry re-enters the next agent as fluid data (the boundary).
    """
    carry = _carry_for(definition)
    context = Context()
    last: RunResult | None = None
    total_cost = 0.0
    for agent in definition.team.agents:
        ctx.cancel_token.raise_if_cancelled()
        run_inputs: dict[str, JSONValue] = {**inputs, **context.to_inputs()}
        last = await runtime.run(
            RunRequest(definition=definition, role=agent.role, inputs=run_inputs), ctx
        )
        total_cost += last.cost_usd
        out = _result_output(
            definition, agent.role, run_inputs, last, upstream_tainted=context.tainted
        )
        # typed-result-out -> delegation-in, keyed so the next agent addresses it
        context = context.add_result(key="prior_result", role=agent.role, result=out)
        context = carry.carry(context)
    if last is None:
        raise ValueError("sequential team has no agents")
    return last.model_copy(update={"cost_usd": total_cost})


async def _run_lead(
    definition: Definition,
    inputs: dict[str, JSONValue],
    ctx: RunContext,
    runtime: AgentRuntime,
) -> RunResult:
    """Lead dispatches its delegates, then combines their typed results via Context.

    Each delegate's typed result is carried as a :class:`ContextEntry` keyed
    ``{role}_result`` (value + taint + lineage), the carry strategy chooses what subset
    the lead sees, and the typed Context is merged into the lead's inputs as fluid data.
    """
    lead = pick_agent(definition, definition.team.lead)
    carry = _carry_for(definition)
    total_cost = 0.0
    context = Context()

    for role in lead.delegates_to:
        ctx.cancel_token.raise_if_cancelled()
        sub = await runtime.run(RunRequest(definition=definition, role=role, inputs=inputs), ctx)
        total_cost += sub.cost_usd
        out = _result_output(definition, role, inputs, sub, upstream_tainted=False)
        # typed result re-enters the lead as fluid data, keyed by delegate role
        context = context.add_result(key=f"{role}_result", role=role, result=out)

    context = carry.carry(context)
    delegated: dict[str, JSONValue] = {**inputs, **context.to_inputs()}
    result = await runtime.run(
        RunRequest(definition=definition, role=lead.role, inputs=delegated), ctx
    )
    total_cost += result.cost_usd
    return result.model_copy(update={"cost_usd": total_cost})
