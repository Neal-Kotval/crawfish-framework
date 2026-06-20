"""A project that ships pipeline.py::build_pipeline() is discovered + run by deploy."""

from __future__ import annotations

from pathlib import Path

from crawfish.deploy import Supervisor, _discover_run_fn, load_trigger, load_workflow
from crawfish.observe import ObserverSurface
from crawfish.store import SqliteStore

_PROJECT = """
from crawfish.nodes import RepoSource
from crawfish.runtime import MockRuntime
from crawfish.workflow import Workflow


def build_pipeline():
    source = RepoSource("repo", config={"repo": "acme/app"})
    return Workflow(steps=[source], name="discovered", runtime=MockRuntime())
"""


def _project(tmp_path: Path) -> Path:
    (tmp_path / "pipeline.py").write_text(_PROJECT)
    return tmp_path


def test_load_workflow_discovers_build_pipeline(tmp_path: Path) -> None:
    wf = load_workflow(_project(tmp_path))
    assert wf is not None
    assert wf.name == "discovered"  # type: ignore[attr-defined]
    assert [s.kind.value for s in wf.steps] == ["source"]  # type: ignore[attr-defined]


def test_missing_pipeline_returns_none(tmp_path: Path) -> None:
    assert load_workflow(tmp_path) is None  # no pipeline.py


def test_discovered_run_fn_executes_a_cycle(tmp_path: Path) -> None:
    store = SqliteStore()
    sup = Supervisor("discovered", store, _discover_run_fn(_project(tmp_path)))
    sup.run_cycle()
    infos = ObserverSurface(store).run_info("discovered")
    assert infos and infos[0].status == "done"


def test_load_trigger_reads_declared_cron(tmp_path: Path) -> None:
    (tmp_path / "pipeline.py").write_text(
        "from crawfish.triggers import CronTrigger\nTRIGGER = CronTrigger('0 8 * * *')\n"
    )
    assert load_trigger(tmp_path) == "0 8 * * *"


def test_load_trigger_reads_plain_schedule(tmp_path: Path) -> None:
    (tmp_path / "pipeline.py").write_text("SCHEDULE = '*/15 * * * *'\n")
    assert load_trigger(tmp_path) == "*/15 * * * *"


def test_load_trigger_none_when_undeclared(tmp_path: Path) -> None:
    (tmp_path / "pipeline.py").write_text("X = 1\n")
    assert load_trigger(tmp_path) is None
