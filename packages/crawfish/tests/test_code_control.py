"""UNFILED-CONTROL — ``craw code cancel`` / ``resume`` over CancelToken + ledger resume.

Pins the M4.5 control plane: ``cancel`` is cooperative (sets a token / signals the
supervisor, never a hard kill); ``resume`` re-enters the ledger resume path and re-charges
**$0** for already-DONE loop items; a cross-org resume sees none of another org's completed
iterations; and the closed exit codes hold (``0`` ok, ``1`` no such run, ``6`` cancel raced
a completed run). No live model calls — temp Store + pre-seeded ledger/surface rows.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from crawfish.code.control import (
    EXIT_RACED_DONE,
    NoSuchRun,
    cancel_run,
    resume_run,
)
from crawfish.core.context import Cancelled, CancelToken
from crawfish.ledger import ExecState, ExecutionLedger
from crawfish.observe import ObserverSurface, RunInfo


def _store(tmp_path: Path):
    from crawfish.store import SqliteStore

    return SqliteStore(tmp_path / "ctl.db")


def _seed_running_batch(
    store,
    *,
    pipeline: str = "triage-bot",
    run_id: str = "run-1",
    items: int = 12,
    done: int = 7,
    org_id: str = "local",
) -> None:
    """Seed a running batch: a RunInfo + ``done`` DONE loop items in the ledger."""
    ObserverSurface(store, org_id=org_id).put_run_info(
        RunInfo(pipeline=pipeline, run_id=run_id, status="running", items=items)
    )
    ledger = ExecutionLedger(store, org_id=org_id)
    for i in range(done):
        ledger.mark_item(pipeline, f"item-{i}", ExecState.DONE)


def test_resume_replays_done_for_free(tmp_path: Path) -> None:
    """resume re-enters the ledger path; completed items re-charge $0, remaining counted."""
    store = _store(tmp_path)
    _seed_running_batch(store, items=12, done=7)
    body = resume_run("run-1", store=store, org_id="local")
    assert body["action"] == "resume"
    assert body["result"] == "resumed"
    assert body["items_replayed_free"] == 7
    assert body["items_remaining"] == 5
    assert body["recharged_usd"] == 0.0
    store.close()


def test_cancel_signals_cooperatively(tmp_path: Path) -> None:
    """cancel sets the caller's CancelToken (cooperative) — a subsequent poll raises."""
    store = _store(tmp_path)
    _seed_running_batch(store)
    token = CancelToken()
    body = cancel_run("run-1", store=store, org_id="local", token=token, stop_supervisor=False)
    assert body["action"] == "cancel"
    assert body["result"] == "cancelled"
    assert token.cancelled  # the token is set — a long loop's raise_if_cancelled will fire
    with pytest.raises(Cancelled):
        token.raise_if_cancelled()
    store.close()


def test_cancel_races_completed_run_is_noop(tmp_path: Path) -> None:
    """A cancel against an already-done run is a no-op (raced_done) — never a hard kill."""
    store = _store(tmp_path)
    ObserverSurface(store).put_run_info(
        RunInfo(pipeline="triage-bot", run_id="run-done", status="done", items=3)
    )
    body = cancel_run("run-done", store=store, stop_supervisor=False)
    assert body["result"] == "raced_done"
    store.close()


def test_unknown_run_raises(tmp_path: Path) -> None:
    """An unknown run id raises NoSuchRun (the CLI maps it to exit 1)."""
    store = _store(tmp_path)
    with pytest.raises(NoSuchRun):
        resume_run("missing", store=store)
    with pytest.raises(NoSuchRun):
        cancel_run("missing", store=store)
    store.close()


def test_cross_org_resume_isolated(tmp_path: Path) -> None:
    """A resume under org A never counts org B's completed loop iterations (tenancy)."""
    store = _store(tmp_path)
    # Org A: run-1 with 7 DONE items. Org B: the SAME pipeline name, but 11 DONE items.
    _seed_running_batch(store, run_id="run-1", items=12, done=7, org_id="a")
    _seed_running_batch(store, run_id="run-2", items=12, done=11, org_id="b")
    body_a = resume_run("run-1", store=store, org_id="a")
    assert body_a["items_replayed_free"] == 7  # only org A's DONE rows, never org B's 11
    assert body_a["items_remaining"] == 5
    # Org A cannot even see org B's run id.
    with pytest.raises(NoSuchRun):
        resume_run("run-2", store=store, org_id="a")
    store.close()


def test_cli_exit_codes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The CLI returns 0 on resume, 6 on a raced cancel, 1 on an unknown run."""
    from crawfish.code.cli import run_code

    store = _store(tmp_path)
    _seed_running_batch(store)
    ObserverSurface(store).put_run_info(
        RunInfo(pipeline="triage-bot", run_id="run-done", status="done", items=3)
    )
    store.close()
    monkeypatch.chdir(tmp_path)
    # Point store_for_dir(".") at the seeded db.
    Path(".crawfish").mkdir(exist_ok=True)
    Path(tmp_path / "ctl.db").rename(Path(".crawfish") / "crawfish.db")

    assert run_code(["resume", "run-1", "--json"]) == 0
    assert run_code(["cancel", "run-done", "--json"]) == EXIT_RACED_DONE
    assert run_code(["resume", "missing", "--json"]) == 1
