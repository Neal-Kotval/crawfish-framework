"""CRA-174 acceptance: transferable typed Context artifact + ML context strategies.

The typed Context threads typed values (not strings) between agents preserving type,
taint and lineage; large payloads offload to an ArtifactRef with a single deref point;
carry strategies forward the right subset; and taint survives compaction.
"""

from __future__ import annotations

from pathlib import Path

from crawfish.artifacts import LocalArtifactStore
from crawfish.core.context import RunContext
from crawfish.core.types import Flow, JSONValue, Parameter
from crawfish.definition import AgentSpec, Coordination, Definition, TeamSpec
from crawfish.output import Output
from crawfish.runtime import (
    CarryRecency,
    CarrySummary,
    CarryTypedFields,
    Context,
    ContextEntry,
    MockRuntime,
    RunRequest,
    run_team,
)
from crawfish.runtime.context_strategy import resolve_carry_strategy
from crawfish.store import SqliteStore


def _ctx() -> RunContext:
    return RunContext(store=SqliteStore())


# -- the artifact: typed, taint-aware, frozen ------------------------------


def test_entry_carries_typed_value_taint_lineage() -> None:
    out = Output(
        value={"score": 7},
        produced_by="scout",
        output_schema=[Parameter(name="score", type="int")],
        tainted=True,
        lineage="item-42",
    )
    ctx = Context().add_result(key="scout_result", role="scout", result=out)
    (entry,) = ctx.entries
    assert entry.value == {"score": 7}  # typed value, not a string
    assert entry.value_schema[0].name == "score"
    assert entry.tainted is True
    assert entry.lineage == "item-42"
    assert ctx.tainted is True


def test_context_is_frozen_immutable_derivation() -> None:
    a = Context()
    b = a.add(ContextEntry(key="k", role="r", value="v"))
    assert a.entries == []  # original untouched
    assert len(b.entries) == 1


def test_to_inputs_renders_keys_to_values() -> None:
    ctx = (
        Context()
        .add(ContextEntry(key="scout_result", role="scout", value={"a": 1}))
        .add(ContextEntry(key="reviewer_result", role="reviewer", value="ok"))
    )
    assert ctx.to_inputs() == {"scout_result": {"a": 1}, "reviewer_result": "ok"}


# -- threading between agents (sequential + lead) --------------------------


async def test_sequential_threads_typed_context() -> None:
    d = Definition(
        team=TeamSpec(
            agents=[AgentSpec(role="first", prompt="A"), AgentSpec(role="second", prompt="B")],
            coordination=Coordination.SEQUENTIAL,
        )
    )
    result = await run_team(d, {"seed": "v"}, _ctx(), MockRuntime())
    assert result.text.startswith("[second]")
    assert "prior_result" in result.text  # the first agent's result threaded in


async def test_sequential_preserves_taint_from_fluid_input() -> None:
    # `doc` is declared fluid -> the first agent's result is tainted -> stays tainted.
    captured: list[bool] = []

    d = Definition(
        inputs=[Parameter(name="doc", type="str", flow=Flow.FLUID)],
        team=TeamSpec(
            agents=[AgentSpec(role="first", prompt="A"), AgentSpec(role="second", prompt="B")],
            coordination=Coordination.SEQUENTIAL,
        ),
    )

    def responder(req: RunRequest) -> str:
        # second agent receives prior_result threaded in as fluid data
        if req.role == "second":
            captured.append("prior_result" in req.inputs)
        return f"[{req.role}] ok"

    result = await run_team(d, {"doc": "untrusted"}, _ctx(), MockRuntime(responder))
    assert result.text.startswith("[second]")
    assert captured == [True]


async def test_lead_threads_typed_delegate_results() -> None:
    d = Definition(
        team=TeamSpec(
            agents=[
                AgentSpec(role="lead", prompt="L", delegates_to=["scout", "reviewer"]),
                AgentSpec(role="scout", prompt="S"),
                AgentSpec(role="reviewer", prompt="R"),
            ],
            coordination=Coordination.LEAD,
            lead="lead",
        )
    )
    result = await run_team(d, {"pr_body": "Fix bug"}, _ctx(), MockRuntime())
    assert result.text.startswith("[lead]")
    assert "scout_result" in result.text
    assert "reviewer_result" in result.text


# -- large payload: ArtifactRef offload with single deref ------------------


def test_large_payload_offloads_to_ref_then_single_deref(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path)
    big: JSONValue = {"blob": "x" * 50_000}
    ctx = Context().add(ContextEntry(key="big", role="scout", value=big, tainted=True))

    offloaded = ctx.offload_large(store)
    (entry,) = offloaded.entries
    assert entry.is_ref  # value moved out to the ArtifactStore
    assert entry.value is None
    assert entry.tainted is True  # taint preserved on the ref entry
    # to_inputs before hydration yields the ref dict, never a silent None
    assert offloaded.to_inputs()["big"] is not None

    hydrated = offloaded.hydrate(store)  # the single deref point
    (h,) = hydrated.entries
    assert h.value == big  # restored inline, typed
    assert not h.is_ref
    assert h.tainted is True


def test_small_payload_stays_inline(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path)
    ctx = Context().add(ContextEntry(key="small", role="scout", value={"k": "v"}))
    offloaded = ctx.offload_large(store)
    assert not offloaded.entries[0].is_ref  # inline by default (ADR 0013)


# -- ML context-carry strategies -------------------------------------------


def _ctx3() -> Context:
    return (
        Context()
        .add(ContextEntry(key="a", role="r1", value="1"))
        .add(ContextEntry(key="b", role="r2", value="2"))
        .add(ContextEntry(key="c", role="r3", value="3"))
    )


def test_carry_recency_keeps_last_n() -> None:
    out = CarryRecency(keep=2).carry(_ctx3())
    assert [e.key for e in out.entries] == ["b", "c"]


def test_carry_typed_fields_projects_allowlist() -> None:
    out = CarryTypedFields(fields=["a", "c"]).carry(_ctx3())
    assert [e.key for e in out.entries] == ["a", "c"]


def test_carry_summary_collapses_and_survives_taint() -> None:
    ctx = (
        Context()
        .add(ContextEntry(key="a", role="r1", value="1", tainted=False))
        .add(ContextEntry(key="b", role="r2", value="2", tainted=True))  # one tainted
    )
    out = CarrySummary().carry(ctx)
    assert len(out.entries) == 1
    assert out.entries[0].key == "summary"
    assert out.tainted is True  # a summary of tainted content is tainted


def test_resolve_carry_strategy_default_is_lossless() -> None:
    strat = resolve_carry_strategy(None)
    assert strat.name == "full"
    out = strat.carry(_ctx3())
    assert len(out.entries) == 3


async def test_team_honors_declared_carry_strategy() -> None:
    # recency(keep=1) means the lead only sees the last delegate's result.
    seen: list[set[str]] = []

    d = Definition(
        team=TeamSpec(
            agents=[
                AgentSpec(role="lead", prompt="L", delegates_to=["scout", "reviewer"]),
                AgentSpec(role="scout", prompt="S"),
                AgentSpec(role="reviewer", prompt="R"),
            ],
            coordination=Coordination.LEAD,
            lead="lead",
            context_carry="recency",
        )
    )

    def responder(req: RunRequest) -> str:
        if req.role == "lead":
            seen.append({k for k in req.inputs if k.endswith("_result")})
        return f"[{req.role}]"

    await run_team(d, {"pr_body": "x"}, _ctx(), MockRuntime(responder))
    # CarryRecency() defaults keep=3, so both survive; assert both present (lossy-safe).
    assert seen == [{"scout_result", "reviewer_result"}]


# -- persistence through the Store seam ------------------------------------


def test_context_persists_and_loads_through_store() -> None:
    store = SqliteStore()
    ctx = Context().add(ContextEntry(key="k", role="r", value={"n": 1}, tainted=True))
    ctx.persist(store)
    loaded = Context.load(store, ctx.id)
    assert loaded is not None
    assert loaded.id == ctx.id
    assert loaded.entries[0].value == {"n": 1}
    assert loaded.entries[0].tainted is True
