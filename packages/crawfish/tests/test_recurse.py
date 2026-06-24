"""CRA-208 (C3) acceptance: bounded self-referential ``recurse``.

Coverage:
  - base case at depth d <= max_depth ⇒ combine folds exactly the children;
  - never-base-case ⇒ halts at max_depth, on_stuck; never exceeds max_depth calls;
  - no max_depth ⇒ UnboundedRecursionError at construction;
  - budget hard-stops; spent reflects every level;
  - replay ⇒ identical descent/combine sequence + folded Output (bit-for-bit);
  - resume at depth k replays 1..k-1 at $0; rows carry org_id;
  - combine taint rule: the folded Output is tainted if ANY child was tainted (union).

Deterministic — a scripted charging runtime meters real spend.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from crawfish.core.context import CostBudget, RunContext
from crawfish.core.types import Flow, JSONValue, Parameter
from crawfish.definition.types import AgentSpec, Coordination, Definition, TeamSpec
from crawfish.nodes.aggregator import collect, count
from crawfish.output import Output, output_content_sha
from crawfish.runtime.base import AgentRuntime, EventKind, RunRequest, RunResult, RuntimeEvent
from crawfish.runtime.replay import RecordReplayRuntime
from crawfish.store import SqliteStore
from crawfish.workflow import UnboundedRecursionError, recurse


def _body() -> Definition:
    return Definition(
        id="planner",
        inputs=[Parameter(name="_recurse_prior", type="str", required=False, flow=Flow.FLUID)],
        team=TeamSpec(
            agents=[AgentSpec(role="planner", prompt="x")], coordination=Coordination.SINGLE
        ),
    )


class _Scripted(AgentRuntime):
    name = "scripted"

    def __init__(self, atomic_at: int, *, cost_per_call: float = 0.01) -> None:
        # Emits {"atomic": True} once ``calls`` reaches ``atomic_at`` (1-indexed level).
        self._atomic_at = atomic_at
        self._cost = cost_per_call
        self.calls = 0

    async def run(self, request: RunRequest, ctx: RunContext) -> RunResult:
        ctx.cancel_token.raise_if_cancelled()
        self.calls += 1
        ctx.cost_budget.charge(self._cost)
        text = json.dumps({"atomic": self.calls >= self._atomic_at, "level": self.calls})
        result = RunResult(
            text=text,
            model="scripted",
            cost_usd=self._cost,
            events=[RuntimeEvent(kind=EventKind.RESULT, text=text)],
        )
        self._emit_telemetry(ctx, result, self.name)
        return result


def _is_atomic(o: Output[JSONValue], depth: int) -> bool:
    # An output-marker base case (ignores the authoritative ``depth``): stops when the body
    # echoes ``{"atomic": true}``.
    raw = o.value
    if isinstance(raw, str):
        raw = json.loads(raw)
    return bool(raw.get("atomic", False)) if isinstance(raw, dict) else False


def _seed() -> Output[JSONValue]:
    return Output(value=json.dumps({"task": "root"}), produced_by="seed", lineage="item-1")


def _ctx(
    store: SqliteStore | None = None, *, limit: float | None = None, org_id: str = "local"
) -> RunContext:
    return RunContext(
        store=store or SqliteStore(), org_id=org_id, cost_budget=CostBudget(limit_usd=limit)
    )


# -- base case + combine ----------------------------------------------------
async def test_base_case_folds_exactly_the_children() -> None:
    rt = _Scripted(atomic_at=3)  # atomic at the 3rd level
    rec = recurse(_body(), base_case=_is_atomic, max_depth=5, combine=collect)
    result = await rec.execute(_seed(), _ctx(), rt)
    assert result.stopped == "base_case"
    assert result.depth_reached == 3
    assert rt.calls == 3
    assert isinstance(result.output.value, list)
    assert len(result.output.value) == 3  # collect folded exactly the 3 descent children


async def test_never_base_case_halts_at_max_depth() -> None:
    rt = _Scripted(atomic_at=99)  # never atomic within the bound
    rec = recurse(_body(), base_case=_is_atomic, max_depth=4, combine=count)
    result = await rec.execute(_seed(), _ctx(), rt)
    assert result.stopped == "max_depth"
    assert result.depth_reached == 4
    assert rt.calls == 4  # never exceeds max_depth calls per path
    assert result.output.value == 4


def test_no_max_depth_raises_at_construction() -> None:
    with pytest.raises(UnboundedRecursionError):
        recurse(_body(), base_case=_is_atomic, max_depth=None, combine=collect)


class _Constant(AgentRuntime):
    """A body that returns a CONSTANT, marker-less Output — it never echoes any depth or
    "atomic" signal. Proves a depth-based base_case must rely on engine state, not output."""

    name = "constant"

    def __init__(self) -> None:
        self.calls = 0

    async def run(self, request: RunRequest, ctx: RunContext) -> RunResult:
        self.calls += 1
        ctx.cost_budget.charge(0.0)
        text = json.dumps({"plan": "subtask"})  # no depth, no "atomic" marker
        result = RunResult(
            text=text,
            model="constant",
            cost_usd=0.0,
            events=[RuntimeEvent(kind=EventKind.RESULT, text=text)],
        )
        self._emit_telemetry(ctx, result, self.name)
        return result


async def test_base_case_receives_authoritative_depth_sequence() -> None:
    """base_case sees the engine-authoritative depth (0, 1, 2, …) regardless of the body
    Output, and a depth-based stop fires even when the Output carries no depth marker."""
    seen_depths: list[int] = []

    def _stop_at_depth_2(o: Output[JSONValue], depth: int) -> bool:
        seen_depths.append(depth)
        return depth >= 2  # stop once the engine reports descent depth 2 (the 3rd level)

    rt = _Constant()  # marker-less body — depth is unknowable from the Output
    rec = recurse(_body(), base_case=_stop_at_depth_2, max_depth=10, combine=collect)
    result = await rec.execute(_seed(), _ctx(), rt)
    assert result.stopped == "base_case"
    assert result.depth_reached == 3  # levels at depth 0, 1, 2 ran
    assert rt.calls == 3
    assert seen_depths == [0, 1, 2]  # authoritative, contiguous, engine-owned


async def test_budget_hard_stops_and_spent_reflects_every_level() -> None:
    rt = _Scripted(atomic_at=99, cost_per_call=0.01)
    ctx = _ctx(limit=0.02)  # room for exactly two levels
    rec = recurse(_body(), base_case=_is_atomic, max_depth=10, combine=count)
    result = await rec.execute(_seed(), ctx, rt)
    assert rt.calls == 2
    assert ctx.cost_budget.spent_usd == pytest.approx(0.02)
    assert result.depth_reached == 2


# -- combine taint = union --------------------------------------------------
async def test_combine_does_not_launder_taint() -> None:
    # A tainted seed makes the descent fluid-derived; the folded Output stays tainted.
    rt = _Scripted(atomic_at=2)
    seed = Output(value=json.dumps({"task": "root"}), produced_by="seed", lineage="i", tainted=True)
    rec = recurse(_body(), base_case=_is_atomic, max_depth=5, combine=collect)
    result = await rec.execute(seed, _ctx(), rt)
    assert result.output.tainted is True  # a vote/fold does not launder taint


# -- determinism + durable resume -------------------------------------------
async def test_replay_identical_descent_and_fold(tmp_path: Path) -> None:
    cassettes = str(tmp_path / "cass")
    rec_rt = RecordReplayRuntime(_Scripted(atomic_at=3), cassettes, record=True)
    rec1 = recurse(_body(), base_case=_is_atomic, max_depth=5, combine=collect)
    out1 = await rec1.execute(_seed(), _ctx(), rec_rt)
    sha1 = output_content_sha(out1.output)

    inner2 = _Scripted(atomic_at=3)
    replay = RecordReplayRuntime(inner2, cassettes, record=False)
    rec2 = recurse(_body(), base_case=_is_atomic, max_depth=5, combine=collect)
    ctx2 = _ctx()
    out2 = await rec2.execute(_seed(), ctx2, replay)
    assert output_content_sha(out2.output) == sha1
    assert ctx2.cost_budget.spent_usd == 0.0


async def test_durable_resume_recharges_zero(tmp_path: Path) -> None:
    from crawfish.ledger import ExecutionLedger

    cassettes = str(tmp_path / "cass")
    store = SqliteStore()
    ledger = ExecutionLedger(store, org_id="local")

    rec_rt = RecordReplayRuntime(_Scripted(atomic_at=3), cassettes, record=True)
    rec1 = recurse(_body(), base_case=_is_atomic, max_depth=5, combine=collect)
    out1 = await rec1.execute(_seed(), _ctx(store), rec_rt, ledger=ledger)
    sha1 = output_content_sha(out1.output)

    inner2 = _Scripted(atomic_at=3)
    replay = RecordReplayRuntime(inner2, cassettes, record=False)
    rec2 = recurse(_body(), base_case=_is_atomic, max_depth=5, combine=collect)
    ctx2 = _ctx(store)
    out2 = await rec2.execute(_seed(), ctx2, replay, ledger=ledger, resume=True)
    assert output_content_sha(out2.output) == sha1
    assert inner2.calls == 0  # every committed depth replayed at $0
    assert ctx2.cost_budget.spent_usd == 0.0


async def test_resume_rows_carry_org_id() -> None:
    from crawfish.ledger import ExecutionLedger

    store = SqliteStore()
    ledger = ExecutionLedger(store, org_id="org-a")
    rt = _Scripted(atomic_at=2)
    rec = recurse(_body(), base_case=_is_atomic, max_depth=5, combine=collect)
    await rec.execute(_seed(), _ctx(store, org_id="org-a"), rt, ledger=ledger)
    assert store.list_records("ledger_loop", org_id="org-b") == []  # cross-org isolation
    assert len(store.list_records("ledger_loop", org_id="org-a")) >= 1
