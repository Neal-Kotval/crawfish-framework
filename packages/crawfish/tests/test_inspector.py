"""CRA-120 acceptance: inspect a run's transcript/tool-calls/cost from the Store,
tail the ledger incrementally, and render a human-readable report — no live model.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from crawfish.core.context import RunContext
from crawfish.definition import Definition
from crawfish.inspector import format_report, inspect_run, tail_events
from crawfish.run import Run, RunStatus
from crawfish.runtime import MockRuntime
from crawfish.store import SqliteStore

FIXTURES = Path(__file__).parent / "fixtures"


def _definition(tmp_path: Path) -> Definition:
    dest = tmp_path / "minimal"
    shutil.copytree(FIXTURES / "minimal", dest, dirs_exist_ok=True)
    return Definition.from_package(str(dest))


async def test_inspect_run_summarizes_a_real_run(tmp_path: Path) -> None:
    d = _definition(tmp_path)
    store = SqliteStore()
    ctx = RunContext(store=store)  # type: ignore[arg-type]
    run = Run(d)
    await run.execute(ctx, MockRuntime())
    assert run.status is RunStatus.DONE

    report = inspect_run(store, ctx.run_id)
    assert report.found is True
    assert report.status == "done"
    # run.start + run.finish spans are present in the transcript.
    span_kinds = {e.kind for e in report.transcript}
    assert "span:run.start" in span_kinds
    assert "span:run.finish" in span_kinds
    # MockRuntime is zero-cost; cost is derivable (and non-negative).
    assert report.cost_usd >= 0.0
    assert report.event_count > 0

    rendered = format_report(report)
    assert rendered  # non-empty
    assert "done" in rendered
    assert ctx.run_id in rendered


def test_tail_events_returns_only_events_after_seq() -> None:
    store = SqliteStore()
    run_id = "run-tail"
    store.append_event(run_id, {"type": "span", "name": "run.start"})
    store.append_event(run_id, {"type": "span", "name": "run.finish", "status": "done"})

    # Poll from the start: everything.
    first = tail_events(store, run_id, after_seq=-1)
    assert len(first) == 2

    # Caller has seen 1 event -> only the second comes back.
    rest = tail_events(store, run_id, after_seq=1)
    assert len(rest) == 1
    assert rest[0]["name"] == "run.finish"

    # Append more and poll incrementally from the prior length.
    store.append_event(run_id, {"event": "runtime.run", "model": "mock", "cost_usd": 0.0})
    new = tail_events(store, run_id, after_seq=2)
    assert len(new) == 1
    assert new[0]["event"] == "runtime.run"

    # Caught up: nothing new.
    assert tail_events(store, run_id, after_seq=3) == []


def test_tool_calls_and_cost_surface_in_report() -> None:
    store = SqliteStore()
    run_id = "run-tools"
    store.append_event(run_id, {"type": "span", "name": "run.start"})
    store.append_event(
        run_id,
        {"kind": "tool_use", "text": "", "tool": {"name": "open_pr", "input": {"repo": "acme"}}},
    )
    store.append_event(run_id, {"event": "runtime.run", "model": "claude", "cost_usd": 0.25})
    store.append_event(
        run_id, {"type": "span", "name": "run.finish", "status": "done", "cost_usd": 0.25}
    )

    report = inspect_run(store, run_id)
    assert report.found is True
    assert report.status == "done"
    assert report.cost_usd == 0.25
    assert len(report.tool_calls) == 1
    assert report.tool_calls[0].name == "open_pr"
    assert report.tool_calls[0].input == {"repo": "acme"}

    rendered = format_report(report)
    assert "open_pr" in rendered
    assert "0.2500" in rendered


def test_inspect_nonexistent_run_returns_empty_report() -> None:
    store = SqliteStore()
    report = inspect_run(store, "does-not-exist")
    assert report.found is False
    assert report.status == "unknown"
    assert report.cost_usd == 0.0
    assert report.event_count == 0
    assert report.tool_calls == []
    assert report.transcript == []
    # format_report must not crash on an empty report.
    rendered = format_report(report)
    assert "not found" in rendered
    assert tail_events(store, "does-not-exist") == []
