"""Parallel fan-out: bounded concurrency, order preservation, budget enforcement."""

from __future__ import annotations

import asyncio
import shutil
import time
from pathlib import Path

import pytest

from crawfish.batch import Batch
from crawfish.core.context import BudgetExceeded, CostBudget, RunContext
from crawfish.core.types import JSONValue, Parameter
from crawfish.definition import Definition
from crawfish.nodes import RepoSource, Source
from crawfish.runtime import MockRuntime
from crawfish.runtime.base import AgentRuntime, EventKind, RunResult, RuntimeEvent
from crawfish.store import SqliteStore

FIXTURES = Path(__file__).parent / "fixtures"


def _definition(tmp_path: Path, tag: str = "full") -> Definition:
    dest = tmp_path / tag
    shutil.copytree(FIXTURES / "full", dest, dirs_exist_ok=True)
    return Definition.from_package(str(dest))


class _BodySource(Source[list[dict[str, JSONValue]]]):
    outputs = [Parameter(name="pr_body", type="str")]
    multi = True

    async def fetch(self, ctx: RunContext):
        from crawfish.output import Output

        return Output(
            output_schema=list(self.outputs), value=self.config["items"], produced_by=self.id
        )


class _SlowRuntime(AgentRuntime):
    """Awaits a fixed delay per call — makes the sequential/parallel gap measurable."""

    name = "slow"

    def __init__(self, delay: float) -> None:
        self._delay = delay

    async def run(self, request, ctx) -> RunResult:
        await asyncio.sleep(self._delay)
        result = RunResult(
            text="ok", cost_usd=0.0, model="slow", events=[RuntimeEvent(kind=EventKind.RESULT)]
        )
        self._emit_telemetry(ctx, result, self.name)
        return result


class _ChargingRuntime(AgentRuntime):
    """Charges a fixed cost per call — drives the budget ceiling."""

    name = "charging"

    def __init__(self, per_call: float) -> None:
        self._per_call = per_call

    async def run(self, request, ctx) -> RunResult:
        ctx.cost_budget.charge(self._per_call)
        result = RunResult(text="ok", cost_usd=self._per_call, model="charging")
        self._emit_telemetry(ctx, result, self.name)
        return result


def _batch(tmp_path: Path, items: list[dict[str, JSONValue]], *, tag: str = "full", **kw) -> Batch:
    d = _definition(tmp_path, tag)
    batch = Batch(d, **kw)
    batch.add_input(RepoSource("repo", config={"repo": "acme/app"}))
    batch.add_input(_BodySource("prs", config={"items": items}))
    return batch


async def test_parallel_runs_all_items_in_order(tmp_path: Path) -> None:
    items = [{"pr_body": f"body-{i}"} for i in range(8)]
    batch = _batch(tmp_path, items, concurrency=4)
    outputs = await batch.run(RunContext(store=SqliteStore()), MockRuntime())
    assert len(outputs) == 8
    assert len(batch.runs) == 8
    # Output order matches input order even though execution interleaved.
    for i, out in enumerate(outputs):
        assert f"body-{i}" in str(out.value)


async def test_parallel_is_faster_than_sequential(tmp_path: Path) -> None:
    items = [{"pr_body": f"b{i}"} for i in range(8)]
    rt = _SlowRuntime(delay=0.05)

    seq = _batch(tmp_path, items, tag="seq", concurrency=1)
    t0 = time.perf_counter()
    await seq.run(RunContext(store=SqliteStore()), rt)
    seq_ms = time.perf_counter() - t0

    par = _batch(tmp_path, items, tag="par", concurrency=8)
    t0 = time.perf_counter()
    await par.run(RunContext(store=SqliteStore()), rt)
    par_ms = time.perf_counter() - t0

    # 8 items x 50ms: sequential ~0.4s, parallel ~0.05s. Allow generous slack.
    assert par_ms < seq_ms / 3


async def test_budget_ceiling_enforced_under_concurrency(tmp_path: Path) -> None:
    items = [{"pr_body": f"b{i}"} for i in range(10)]
    budget = CostBudget(limit_usd=0.03)  # ~3 calls at $0.01 before breach
    batch = _batch(tmp_path, items, concurrency=4, cost_budget=budget)
    with pytest.raises(BudgetExceeded):
        await batch.run(RunContext(store=SqliteStore()), _ChargingRuntime(0.01))


async def test_continue_on_error_records_anomaly(tmp_path: Path) -> None:
    class _FlakyRuntime(AgentRuntime):
        name = "flaky"

        async def run(self, request, ctx) -> RunResult:
            body = str(request.inputs.get("pr_body", ""))
            if body == "boom":
                raise RuntimeError("intentional failure")
            return RunResult(text="ok", model="flaky")

    items = [{"pr_body": "ok-1"}, {"pr_body": "boom"}, {"pr_body": "ok-2"}]
    batch = _batch(tmp_path, items, concurrency=3, continue_on_error=True)
    outputs = await batch.run(RunContext(store=SqliteStore()), _FlakyRuntime())
    assert len(outputs) == 2  # the failed item leaves no output
    assert any(a.kind == "run_failed" for a in batch.detect_anomalies())
