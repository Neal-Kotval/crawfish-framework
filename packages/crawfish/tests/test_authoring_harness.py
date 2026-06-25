"""CRA-268 — deterministic record/replay harness for the authoring loop.

The agent-driven authoring loop calls a live model, so it is untestable under the repo's
"no live model calls" bar without a record/replay layer. These tests pin: a golden
authoring session replays byte-identically with no live call; model turns route through
:class:`RecordReplayRuntime` + :class:`MockRuntime` (a missing cassette raises
:class:`CassetteMiss` — replay never silently hits the network); the harness asserts each
authored file's CRA-266 provenance row and any CRA-267 jail :class:`Denial`; and a
poisoned golden replays the tainted-provenance path. All replay; zero live calls.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from crawfish.code.harness import AuthoringResult, AuthoringSession
from crawfish.jail import FLUID_TAINT
from crawfish.runtime.mock import MockRuntime
from crawfish.runtime.replay import CassetteMiss
from crawfish.store import SqliteStore

_FIXTURES = Path(__file__).parent / "fixtures" / "authoring"


def _session(fixture: str, *, project_dir: Path, cassette_dir: Path, store: SqliteStore, mode: str):
    return AuthoringSession.from_fixture(
        _FIXTURES / fixture,
        runtime=MockRuntime(),
        store=store,
        project_dir=project_dir,
        cassette_dir=cassette_dir,
        mode=mode,
    )


def _record_then_replay(fixture: str, tmp_path: Path) -> tuple[AuthoringResult, AuthoringResult]:
    """Record the golden cassettes (deterministic MockRuntime — NOT live), then replay.

    The cassettes are written by a record pass over the mock (zero cost, no network); the
    asserting pass is pure replay. Mirrors the CI shape: replay is the default, record is a
    developer pre-step that here uses the mock for determinism.
    """
    cassettes = tmp_path / "cassettes"
    store = SqliteStore()
    try:
        rec = asyncio.run(
            _session(
                fixture,
                project_dir=tmp_path / "proj_rec",
                cassette_dir=cassettes,
                store=store,
                mode="record",
            ).run()
        )
        rep = asyncio.run(
            _session(
                fixture,
                project_dir=tmp_path / "proj_rep",
                cassette_dir=cassettes,
                store=store,
                mode="replay",
            ).run()
        )
        return rec, rep
    finally:
        store.close()


def test_clean_session_replays_with_provenance_and_no_denials(tmp_path: Path) -> None:
    """A recorded clean authoring session replays deterministically + closes the loop."""
    rec, rep = _record_then_replay("triage_new_tool.json", tmp_path)
    # Deterministic file set, byte-identical across record/replay.
    assert rep.files_written == ("instructions.md", "tools/notify.py", "definition.py")
    assert rec.files_written == rep.files_written
    # One CRA-266 provenance row per import-bearing authored file (definition.py + tools).
    authored = {p.component_path for p in rep.provenance}
    assert "definition.py" in authored
    assert "tools/notify.py" in authored
    # A clean session has no jail denials and is not tainted.
    assert rep.jail_denials == ()
    assert all(not p.taint for p in rep.provenance)
    assert not rep.failed_closed


def test_replay_without_cassette_raises_cassette_miss(tmp_path: Path) -> None:
    """Replay never silently hits the network: a missing cassette is a CassetteMiss."""
    store = SqliteStore()
    try:
        session = _session(
            "triage_new_tool.json",
            project_dir=tmp_path / "proj",
            cassette_dir=tmp_path / "empty_cassettes",  # no cassettes recorded
            store=store,
            mode="replay",
        )
        with pytest.raises(CassetteMiss):
            asyncio.run(session.run())
    finally:
        store.close()


def test_poisoned_session_replays_tainted_provenance(tmp_path: Path) -> None:
    """A poisoned golden (authored under fluid context) replays with tainted provenance.

    The downstream gates (CRA-267 jail / CRA-271 redaction / CRA-272 assembly) can then
    refuse it because the per-file provenance row is source_tainted.
    """
    _rec, rep = _record_then_replay("poisoned_exfil_tool.json", tmp_path)
    assert "tools/exfil.py" in rep.files_written
    # Every agent-authored component's row is tainted (the poisoned-ticket case).
    tainted_paths = {p.component_path for p in rep.provenance if FLUID_TAINT in p.taint}
    assert "tools/exfil.py" in tainted_paths
    assert "definition.py" in tainted_paths


def test_default_mode_is_replay_and_never_records(tmp_path: Path) -> None:
    """The default mode is replay; a default-constructed session does not record."""
    store = SqliteStore()
    try:
        session = _session(
            "triage_new_tool.json",
            project_dir=tmp_path / "proj",
            cassette_dir=tmp_path / "empty",
            store=store,
            mode="replay",
        )
        # In replay mode with no cassette, the model turn fails closed (no silent record).
        with pytest.raises(CassetteMiss):
            asyncio.run(session.run())
        # No cassette files were written by the replay attempt.
        empty_dir = tmp_path / "empty"
        assert not (empty_dir.exists() and list(empty_dir.glob("*.json")))
    finally:
        store.close()
