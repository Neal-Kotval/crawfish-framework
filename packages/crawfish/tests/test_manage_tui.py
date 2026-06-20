"""Pure-render helpers behind the interactive `craw manage` TUI (no curses needed)."""

from __future__ import annotations

from crawfish.manage import (
    PipelineStatus,
    recent_messages,
    run_feed,
    stats_lines,
    workflow_diagram,
)
from crawfish.observe import RunInfo


def test_workflow_diagram_boxes_and_arrows() -> None:
    out = workflow_diagram([("source", "linear-issues"), ("batch", "draft"), ("sink", "github-pr")])
    assert "SOURCE" in out and "BATCH" in out and "SINK" in out
    assert "linear-issues" in out and "github-pr" in out
    assert "-->" in out  # arrows join adjacent stages
    assert out.count("\n") == 3  # top / kind / name / bottom


def test_workflow_diagram_empty_is_safe() -> None:
    assert "unavailable" in workflow_diagram([])


def test_stats_lines_report_optional() -> None:
    row = PipelineStatus(name="p", status="running", pid=1, cost_today_usd=0.5)
    lines = stats_lines(row, None)
    assert any("status:" in line and "running" in line for line in lines)
    assert any("cost today: $0.5000" in line for line in lines)


def test_recent_messages_without_report() -> None:
    assert recent_messages(None) == ["(no run yet)"]


def test_run_feed_summarizes_recent_runs() -> None:
    runs = [
        RunInfo(pipeline="p", run_id="abcd1234ef", status="done", cost_usd=0.0, started_at=1.0),
        RunInfo(pipeline="p", run_id=" wxyz9876", status="failed", cost_usd=0.0, started_at=2.0),
    ]
    feed = run_feed(runs, 5)
    assert len(feed) == 2
    assert feed[0].startswith("done")
    assert "(abcd1234" in feed[0]  # run id is abbreviated


def test_run_feed_empty() -> None:
    assert run_feed([]) == ["(no runs yet)"]
