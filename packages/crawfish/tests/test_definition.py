"""CRA-102 acceptance: the directory compiler produces typed Definitions."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from crawfish.core import Flow
from crawfish.definition import Coordination, Definition, DefinitionLoadError, load_definition

FIXTURES = Path(__file__).parent / "fixtures"


def _copy(name: str, dest: Path) -> Path:
    target = dest / name
    shutil.copytree(FIXTURES / name, target)
    return target


def test_minimal_instructions_only_compiles_to_one_agent(tmp_path: Path) -> None:
    d = load_definition(_copy("minimal", tmp_path))
    assert len(d.team.agents) == 1
    assert d.team.agents[0].role == "main"
    assert d.team.coordination is Coordination.SINGLE
    assert d.version.sha is not None  # content-derived


def test_full_fixture_compiles_all_artifacts(tmp_path: Path) -> None:
    d = load_definition(_copy("full", tmp_path))
    roles = {a.role for a in d.team.agents}
    assert roles == {"lead", "scout", "reviewer"}
    assert d.id == "pr-reviewer"  # identity from pyproject name
    assert d.team.coordination is Coordination.LEAD
    assert d.team.lead == "lead"
    # assets
    assert d.assets.skills == ["triage.md"]
    assert {c.name for c in d.assets.mcp} == {"linear"}
    assert {p.name for p in d.assets.policies} == {"spend_cap"}


def test_tool_name_from_filename_no_wiring(tmp_path: Path) -> None:
    d = load_definition(_copy("full", tmp_path))
    scout = d.agent("scout")
    assert scout is not None
    assert "open_pr" in scout.tools


def test_per_agent_model_pin_respected(tmp_path: Path) -> None:
    d = load_definition(_copy("full", tmp_path))
    assert d.agent("reviewer").model == "claude-opus-4-8"  # pinned
    assert d.agent("scout").model is None  # unpinned -> platform picks


def test_delegates_and_policy_bindings(tmp_path: Path) -> None:
    d = load_definition(_copy("full", tmp_path))
    assert d.agent("lead").delegates_to == ["scout", "reviewer"]
    assert d.agent("reviewer").policies == ["spend_cap"]


def test_broken_binding_fails_at_load(tmp_path: Path) -> None:
    with pytest.raises(DefinitionLoadError):
        load_definition(_copy("broken", tmp_path))


def test_typed_io_keeps_flow_tags_through_json(tmp_path: Path) -> None:
    d = load_definition(_copy("full", tmp_path))
    again = Definition.model_validate_json(d.model_dump_json())
    by_name = {p.name: p for p in again.inputs}
    assert by_name["repo"].flow is Flow.STATIC
    assert by_name["pr_body"].flow is Flow.FLUID


def test_recompile_is_byte_stable(tmp_path: Path) -> None:
    path = _copy("full", tmp_path)
    first = load_definition(path).model_dump_json()
    second = load_definition(path).model_dump_json()  # lock now exists; excluded from hash
    assert first == second


def test_directory_and_copy_compile_identically(tmp_path: Path) -> None:
    # canonical loader => path-independent identity (ADR 0006)
    a = load_definition(_copy("full", tmp_path / "a"))
    b = load_definition(_copy("full", tmp_path / "b"))
    assert a.model_dump_json() == b.model_dump_json()


def test_from_package_and_export(tmp_path: Path) -> None:
    d = Definition.from_package(str(_copy("full", tmp_path)))
    pkg = d.export()
    assert pkg.id == "pr-reviewer"
    assert pkg.checksum
    assert pkg.definition["id"] == "pr-reviewer"


def test_lock_written(tmp_path: Path) -> None:
    path = _copy("minimal", tmp_path)
    load_definition(path)
    assert (path / "definition.lock").exists()
