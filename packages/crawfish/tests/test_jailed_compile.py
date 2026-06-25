"""CRA-267 — compile agent-authored code in the jail, not authoring-time-trusted.

``load_definition`` imports ``definition.py`` / ``tools/*.py`` in-process at compile
time — arbitrary code execution in the orchestrator when the author is ``craw code``.
These tests pin the jailed compile path: a clean agent-authored compile returns the
typed shape and records taint; a folder-escape or egress probe fails closed with a
``JAIL_VIOLATION`` + :class:`DefinitionLoadError`; the human path is untouched; a
FLUID ``allow_paths`` entry raises :class:`StaticOnlyError` before any spawn. All via
:class:`FakeJail` (``SandboxPolicy(kind="fake")``) — no real process, no model call.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest

from crawfish.core.types import Flow
from crawfish.definition.compiler import DefinitionLoadError
from crawfish.definition.jailed import import_bearing_files, load_definition_jailed
from crawfish.emission import EmissionKind, read_emissions
from crawfish.jail import (
    FLUID_TAINT,
    JailPath,
    PathMode,
    SandboxPolicy,
    StaticOnlyError,
    _Probe,
    select_jail,
)
from crawfish.provenance import component_tainted, file_provenance
from crawfish.store import SqliteStore


def _project(tmp_path: Path, *, tool_body: str = "def notify(x):\n    return x\n") -> Path:
    """A minimal agent-authorable Definition dir: instructions + a tool + typed IO."""
    root = tmp_path / "triage"
    (root / "tools").mkdir(parents=True)
    (root / "instructions.md").write_text("You triage tickets.\n")
    (root / "tools" / "notify.py").write_text(tool_body)
    (root / "definition.py").write_text(
        "from crawfish.core.types import Flow, Parameter\n"
        "inputs = [Parameter(name='ticket', type='str', flow=Flow.FLUID)]\n"
        "outputs = [Parameter(name='label', type='str', flow=Flow.STATIC)]\n"
    )
    return root


def test_import_bearing_files_enumerates_the_compile_surface(tmp_path: Path) -> None:
    root = _project(tmp_path)
    files = import_bearing_files(root)
    assert "definition.py" in files
    assert "tools/notify.py" in files


def test_clean_agent_compile_returns_typed_shape_and_records_provenance(tmp_path: Path) -> None:
    """A clean jailed compile returns the typed Definition and stamps per-file provenance."""
    root = _project(tmp_path)
    store = SqliteStore()
    try:
        result = load_definition_jailed(root, store=store, policy=SandboxPolicy(kind="fake"))
        # The typed shape survives the boundary (inputs/outputs reflect definition.py).
        assert [p.name for p in result.definition.inputs] == ["ticket"]
        assert [p.name for p in result.definition.outputs] == ["label"]
        # One per-file provenance row per import-bearing file, authored_by craw-code.
        authored = {r.component_path: r.authored_by for r in result.provenance}
        assert authored["definition.py"] == "craw-code"
        assert authored["tools/notify.py"] == "craw-code"
        # A clean compile is not tainted.
        assert result.out_taint == frozenset()
    finally:
        store.close()


def test_parameters_compatible_holds_after_jailed_compile(tmp_path: Path) -> None:
    """Type wiring behaves identically to in-process compile (registry round-trips)."""
    from crawfish.core import parameters_compatible

    root = _project(tmp_path)
    store = SqliteStore()
    try:
        result = load_definition_jailed(root, store=store, policy=SandboxPolicy(kind="fake"))
        out = result.definition.outputs[0]
        # A static 'str' output wires into a static 'str' input slot — structural check.
        from crawfish.core.types import Parameter

        sink_slot = Parameter(name="label", type="str", flow=Flow.STATIC)
        assert parameters_compatible(out, sink_slot)
    finally:
        store.close()


def _hostile_probe(reads: Sequence[str] = (), connects: Sequence[str] = ()):  # type: ignore[no-untyped-def]
    def _factory(_files: Sequence[str], _root: Path):  # type: ignore[no-untyped-def]
        def _program(_cmd: Sequence[str]) -> _Probe:
            return _Probe(reads=list(reads), connects=list(connects))

        return _program

    return _factory


def test_folder_escape_fails_closed_with_jail_violation(tmp_path: Path) -> None:
    """A tool whose import reads /etc/shadow → Denial + JAIL_VIOLATION + DefinitionLoadError."""
    root = _project(tmp_path)
    store = SqliteStore()
    try:
        with pytest.raises(DefinitionLoadError):
            load_definition_jailed(
                root,
                store=store,
                policy=SandboxPolicy(kind="fake"),
                compile_probe=_hostile_probe(reads=["/etc/shadow"]),
            )
        # The denial was audited as a JAIL_VIOLATION emission.
        emissions = read_emissions(store, f"jailed-compile:{root.name}")
        assert any(e.kind is EmissionKind.JAIL_VIOLATION for e in emissions)
        # Fail-closed: no provenance row was written (the authored code never ran).
        assert file_provenance("tools/notify.py", "", store=store) is None
    finally:
        store.close()


def test_egress_under_deny_net_fails_closed(tmp_path: Path) -> None:
    """A tool whose import opens a socket under allow_net=False fails closed."""
    root = _project(tmp_path)
    store = SqliteStore()
    try:
        with pytest.raises(DefinitionLoadError):
            load_definition_jailed(
                root,
                store=store,
                policy=SandboxPolicy(kind="fake"),
                compile_probe=_hostile_probe(connects=["evil.example.com:443"]),
            )
        emissions = read_emissions(store, f"jailed-compile:{root.name}")
        assert any(e.kind is EmissionKind.JAIL_VIOLATION for e in emissions)
    finally:
        store.close()


def test_out_taint_is_recorded_onto_provenance(tmp_path: Path) -> None:
    """A jailed compile whose child emitted fluid taint records it on the file rows."""
    root = _project(tmp_path)
    store = SqliteStore()

    def _fluid_probe(files: Sequence[str], r: Path):  # type: ignore[no-untyped-def]
        # In-scope reads (no denial) but the child declares its output derives from fluid.
        reads = [str((r / f).resolve()) for f in files]

        def _program(_cmd: Sequence[str]) -> _Probe:
            return _Probe(reads=reads, emits_fluid=True)

        return _program

    try:
        result = load_definition_jailed(
            root, store=store, policy=SandboxPolicy(kind="fake"), compile_probe=_fluid_probe
        )
        assert FLUID_TAINT in result.out_taint
        # Every agent-authored component's row carries the taint (and is monotonic).
        assert component_tainted(
            "tools/notify.py",
            next(r.content_sha for r in result.provenance if r.component_path == "tools/notify.py"),
            store=store,
        )
    finally:
        store.close()


def test_human_authored_components_are_not_taint_carriers(tmp_path: Path) -> None:
    """A file mapped to 'human' is confined but stamped human-authored (no agent taint)."""
    root = _project(tmp_path)
    store = SqliteStore()
    try:
        result = load_definition_jailed(
            root,
            store=store,
            policy=SandboxPolicy(kind="fake"),
            authored_by=lambda f: "human" if f == "definition.py" else "craw-code",
        )
        rows = {r.component_path: r for r in result.provenance}
        assert rows["definition.py"].authored_by == "human"
        assert not rows["definition.py"].taint
    finally:
        store.close()


def test_fluid_allow_path_raises_static_only_before_spawn() -> None:
    """A FLUID-tagged allow_paths entry is rejected by the jail before any process spawns."""
    jail = select_jail(SandboxPolicy(kind="fake"))
    with pytest.raises(StaticOnlyError):
        jail.run(
            ["python", "-c", "compile-probe"],
            allow_paths=[JailPath("/some/dir", mode=PathMode.RO, flow=Flow.FLUID)],
            allow_net=False,
        )
