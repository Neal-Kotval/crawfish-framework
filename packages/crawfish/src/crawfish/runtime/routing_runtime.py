"""RoutingRuntime — apply a :class:`~crawfish.routing.RoutingPolicy` before delegating.

CRA-182's runtime leg of smart routing. A thin :class:`AgentRuntime` wrapper: for each
request it asks the shared routing decision (``crawfish.routing.route_decision``) which
model the step should run, **pins that model on the request**, optionally emits a typed
routing :class:`~crawfish.emission.Emission`, then hands off to the wrapped inner runtime
(typically a :class:`~crawfish.runtime.provider_runtime.ProviderRuntime`, so a routed
``"local"`` field reaches the :class:`~crawfish.runtime.local_provider.LocalHTTPProvider`).

It does **not** re-resolve models itself: pinning ``request.model`` to the
already-resolved id means the inner runtime's own ``resolve_model`` call is a no-op pass
through (a concrete id resolves to itself). The decision is made once, in
``crawfish.routing``, through the single shared resolver — the same one
:func:`crawfish.cost.estimate_cost` uses — so preview and run can't drift (CRA-186).

Deterministic and live-call-free: routing is pure; the inner runtime is whatever the
test injects (MockProvider / RecordReplay / a mock-transport LocalHTTPProvider).
"""

from __future__ import annotations

from crawfish.core.context import RunContext
from crawfish.provider import ModelsConfig
from crawfish.routing import RoutingPolicy, route_decision, routing_emission
from crawfish.runtime.base import AgentRuntime, RunRequest, RunResult

__all__ = ["RoutingRuntime"]


class RoutingRuntime(AgentRuntime):
    """Pin the policy-routed model on each request, then delegate to ``inner``.

    A per-run ``request.model`` override is honoured untouched (an explicit pin wins over
    routing). When ``emit_decision`` is set, a ``MODEL`` emission recording *why* the
    model was chosen is written before the inner run (its ``cost_usd`` is 0.0; the real
    spend is charged by the inner runtime).
    """

    name = "routing"

    def __init__(
        self,
        inner: AgentRuntime,
        policy: RoutingPolicy,
        *,
        default_model: str,
        config: ModelsConfig | None = None,
        emit_decision: bool = False,
    ) -> None:
        self._inner = inner
        self._policy = policy
        self._default_model = default_model
        self._config = config
        self._emit_decision = emit_decision

    def _route(self, request: RunRequest, ctx: RunContext) -> RunRequest:
        if request.model:  # an explicit per-run pin bypasses routing
            return request
        decision = route_decision(
            request.definition,
            request.role,
            policy=self._policy,
            default=self._default_model,
            config=self._config,
        )
        if self._emit_decision:
            from crawfish.emission import emit

            emit(
                ctx.store,
                routing_emission(decision, run_id=ctx.run_id, org_id=ctx.org_id),
                org_id=ctx.org_id,
            )
        return request.model_copy(update={"model": decision.resolved})

    async def run(self, request: RunRequest, ctx: RunContext) -> RunResult:
        ctx.cancel_token.raise_if_cancelled()
        return await self._inner.run(self._route(request, ctx), ctx)
