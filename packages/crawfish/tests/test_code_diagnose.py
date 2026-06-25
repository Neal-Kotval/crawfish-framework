"""UNFILED-DIAGNOSE — ``craw code diagnose <run_id>``: ledger + DLQ + observer → root cause.

Pins: correlates RunInfo + observer events + DLQ + the failing-node emission for a run; identifies
the first failing node + an error class; emits a concrete ``craw replay --swap <run_id>`` $0
remediation; the DLQ read is read-only (never drained); the failing-IO ``detail`` is output-encoded;
and the run lookup is org-isolated. Read-only + scrubbed; no live model call.
"""

from __future__ import annotations

import time
from pathlib import Path

from crawfish.code.dashboard import build_data
from crawfish.code.diagnose import diagnose_run
from crawfish.emission import Emission, EmissionKind, emit
from crawfish.manage import store_for_dir
from crawfish.observe import ObserverEvent, ObserverSurface, RunInfo, Severity


def _seed(root: Path, *, org: str = "local") -> None:
    """Seed a failed run: RunInfo + a failure.rate event + a DLQ entry + a failing SINK emission."""
    store = store_for_dir(str(root))
    try:
        surface = ObserverSurface(store, org_id=org)
        surface.put_run_info(
            RunInfo(pipeline="triage-bot", run_id="run-1", status="failed", version="0.3.1")
        )
        surface.emit(
            ObserverEvent(
                pipeline="triage-bot",
                kind="failure.rate",
                severity=Severity.CRITICAL,
                detail="3 of 5 failed",
                run_id="run-1",
                ts=time.time() - 30,
            )
        )
        emit(
            store,
            Emission(
                kind=EmissionKind.SINK,
                run_id="run-1",
                org_id=org,
                pipeline="triage-bot",
                node_id="summarize",
                attrs={"target": "slack", "committed": False},
            ),
            org_id=org,
        )
        store.put_record(
            "dead_letter",
            "run-1:ticket-42",
            {
                "batch_id": "run-1",
                "item_id": "ticket-42",
                "error": "schema mismatch: invalid label",
                "pipeline": "triage-bot",
                "run_id": "run-1",
            },
            org_id=org,
        )
    finally:
        store.close()


def test_diagnose_correlates_first_failure_and_dlq(tmp_path: Path) -> None:
    root = tmp_path / "app"
    (root / ".crawfish").mkdir(parents=True)
    _seed(root)
    data = build_data(root, org_id="local")
    try:
        d = diagnose_run(data, "run-1")
    finally:
        data._store.close()  # noqa: SLF001

    assert d is not None
    assert d.status == "failed"
    assert d.pipeline == "triage-bot"
    assert d.first_failure.node == "summarize"  # from the SINK emission's static node_id
    assert d.first_failure.error_class == "validation"  # "schema"/"invalid" → validation bucket
    assert d.first_failure.item_id == "ticket-42"
    assert d.dlq[0]["item_id"] == "ticket-42"
    assert "failure.rate" in d.observer_events
    # The remediation is the exact $0 replay --swap for this run.
    assert d.remediation.action == "replay_swap"
    assert d.remediation.command == "craw replay --swap model=<candidate-model> run-1"
    assert d.remediation.estimated_usd == 0.0


def test_diagnose_dlq_read_is_read_only(tmp_path: Path) -> None:
    root = tmp_path / "app"
    (root / ".crawfish").mkdir(parents=True)
    _seed(root)
    data = build_data(root, org_id="local")
    try:
        diagnose_run(data, "run-1")
    finally:
        data._store.close()  # noqa: SLF001
    # The DLQ entry is untouched after diagnose (never drained/deleted).
    store = store_for_dir(str(root))
    try:
        rows = store.list_records("dead_letter", org_id="local")
    finally:
        store.close()
    assert len(rows) == 1
    assert rows[0]["item_id"] == "ticket-42"


def test_diagnose_unknown_run_returns_none(tmp_path: Path) -> None:
    root = tmp_path / "app"
    (root / ".crawfish").mkdir(parents=True)
    _seed(root)
    data = build_data(root, org_id="local")
    try:
        assert diagnose_run(data, "no-such-run") is None
    finally:
        data._store.close()  # noqa: SLF001


def test_diagnose_two_org_isolation(tmp_path: Path) -> None:
    root = tmp_path / "app"
    (root / ".crawfish").mkdir(parents=True)
    _seed(root, org="a")
    data = build_data(root, org_id="b")  # org b never sees org a's run
    try:
        assert diagnose_run(data, "run-1") is None
    finally:
        data._store.close()  # noqa: SLF001
