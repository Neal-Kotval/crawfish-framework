"""EscalatingRuntime — confidence-gated cascade over a single inner runtime."""

from __future__ import annotations

import json

from crawfish.core.context import RunContext
from crawfish.emission import EmissionKind, read_emissions
from crawfish.runtime.base import AgentRuntime, RunRequest, RunResult
from crawfish.runtime.escalate import EscalatingRuntime, confidence_below
from crawfish.store import SqliteStore


class _ModelAwareRuntime(AgentRuntime):
    """Returns a payload whose confidence depends on which model was pinned.

    The cheap model answers with low confidence; the strong model is confident. Lets us
    assert the cascade both *triggers* and *takes the strong result*.
    """

    name = "model-aware"

    def __init__(self, *, cheap: str, strong: str) -> None:
        self._cheap = cheap
        self._strong = strong

    async def run(self, request: RunRequest, ctx: RunContext) -> RunResult:
        model = request.model or ""
        if model == self._strong:
            text = json.dumps({"answer": "strong", "confidence": 0.95})
        else:
            text = json.dumps({"answer": "cheap", "confidence": 0.30})
        result = RunResult(text=text, model=model, cost_usd=0.0)
        self._emit_telemetry(ctx, result, self.name)
        return result


def _rt() -> EscalatingRuntime:
    inner = _ModelAwareRuntime(cheap="haiku", strong="opus")
    return EscalatingRuntime(
        inner,
        primary_model="haiku",
        strong_model="opus",
        should_escalate=confidence_below(0.7),
    )


def _request() -> RunRequest:
    from crawfish.definition.types import AgentSpec, Coordination, Definition, TeamSpec

    d = Definition(
        id="t",
        team=TeamSpec(agents=[AgentSpec(role="a", prompt="x")], coordination=Coordination.SINGLE),
    )
    return RunRequest(definition=d, role="a", inputs={})


async def test_escalates_when_primary_unsure() -> None:
    store = SqliteStore()
    rt = _rt()
    ctx = RunContext(store=store)
    result = await rt.run(_request(), ctx)
    assert "strong" in result.text  # the strong model's answer is returned
    assert result.model == "opus"
    assert rt.escalations == 1
    assert rt.calls == 2
    # Both tiers are visible on the ledger: two MODEL emissions for this run.
    models = [
        e.attrs["model"] for e in read_emissions(store, ctx.run_id) if e.kind is EmissionKind.MODEL
    ]
    assert models == ["haiku", "opus"]


async def test_no_escalation_when_primary_confident() -> None:
    store = SqliteStore()
    # Cheap model is confident here → no escalation.
    inner = _ModelAwareRuntime(cheap="haiku", strong="opus")

    class _Confident(_ModelAwareRuntime):
        async def run(self, request: RunRequest, ctx: RunContext) -> RunResult:
            text = json.dumps({"answer": "cheap", "confidence": 0.99})
            result = RunResult(text=text, model=request.model or "", cost_usd=0.0)
            self._emit_telemetry(ctx, result, self.name)
            return result

    rt = EscalatingRuntime(
        _Confident(cheap="haiku", strong="opus"),
        primary_model="haiku",
        strong_model="opus",
        should_escalate=confidence_below(0.7),
    )
    result = await rt.run(_request(), RunContext(store=store))
    assert result.model == "haiku"
    assert rt.escalations == 0
    assert rt.calls == 1
    _ = inner


async def test_escalates_on_unparseable_output() -> None:
    class _Garbage(AgentRuntime):
        name = "garbage"

        async def run(self, request: RunRequest, ctx: RunContext) -> RunResult:
            if request.model == "opus":
                return RunResult(text=json.dumps({"ok": True, "confidence": 1.0}), model="opus")
            return RunResult(text="not json at all", model="haiku")

    rt = EscalatingRuntime(
        _Garbage(),
        primary_model="haiku",
        strong_model="opus",
        should_escalate=confidence_below(0.7),
    )
    result = await rt.run(_request(), RunContext(store=SqliteStore()))
    assert result.model == "opus"
    assert rt.escalations == 1
