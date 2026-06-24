"""CRA-171 acceptance: the typed emission substrate.

Covers the behavioural half landed on top of the CRA-184 frozen contract:
``to_event``/``from_event`` round-trip, the legacy-dict back-compat shim,
``read_emissions`` over a mixed ledger, taint propagation, the per-run volume cap,
and the load-bearing security invariant (a secret value never lands in the ledger).

Deterministic: no live model call, no wall-clock read — ``ts`` is passed in.
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

from crawfish.core.context import RunContext
from crawfish.emission import (
    EMISSION_SCHEMA_VERSION,
    REQUIRED_ATTRS,
    Emission,
    EmissionKind,
    emit,
    read_emissions,
)
from crawfish.secrets import ScrubbingStore
from crawfish.store import SqliteStore


def _valid_attrs(kind: EmissionKind) -> dict[str, object]:
    """Minimal attrs that satisfy a kind's REQUIRED_ATTRS (typed sample values)."""
    samples: dict[str, object] = {
        "runtime": "mock",
        "status": "done",
        "model": "claude",
        "cost_usd": 0.01,
        "tool": "search",
        "target": "github_pr",
        "committed": True,
        "strategy": "linear_compact",
        "kind": "cost.spike",
        "severity": "warn",
        "metric": "latency_ms",
        "value": 12.0,
        "ref": "GITHUB_TOKEN",
        "node_id": "node-1",
        "attempt": "egress:evil.com",
        "correction_type": "human_revert",
        "provenance": "trusted",
    }
    return {key: samples[key] for key in REQUIRED_ATTRS[kind]}


# -- round-trip ---------------------------------------------------------------
def test_to_event_from_event_round_trip() -> None:
    em = Emission(
        kind=EmissionKind.MODEL,
        run_id="r1",
        node_id="agent-1",
        ts=123.0,
        attrs={"model": "claude", "cost_usd": 0.02},
        tainted=True,
    )
    event = em.to_event()
    assert event["kind"] == "model"  # enum serialized to its value string
    assert event["tainted"] is True
    assert Emission.from_event(event) == em


def test_every_kind_round_trips() -> None:
    for kind in EmissionKind:
        em = Emission(kind=kind, run_id="r1", ts=1.0, attrs=_valid_attrs(kind))
        assert em.is_valid(), f"{kind} sample attrs incomplete"
        rehydrated = Emission.from_event(em.to_event())
        assert rehydrated == em
        assert rehydrated.kind is kind


# -- legacy back-compat shim --------------------------------------------------
def test_legacy_runtime_run_lifts_to_model() -> None:
    legacy = {
        "event": "runtime.run",
        "runtime": "command",
        "model": "claude",
        "cost_usd": 0.05,
        "events": 3,
        "session_id": "sess-1",
    }
    em = Emission.from_event(legacy)
    assert em.kind is EmissionKind.MODEL
    assert em.attrs["model"] == "claude"
    assert em.attrs["cost_usd"] == 0.05
    assert em.schema_version == EMISSION_SCHEMA_VERSION


def test_legacy_span_lifts_to_run_start_and_finish() -> None:
    start = Emission.from_event(
        {"type": "span", "name": "run.start", "trace_id": "t", "definition": "d"}
    )
    assert start.kind is EmissionKind.RUN_START

    finish = Emission.from_event(
        {"type": "span", "name": "run.finish", "status": "failed", "latency_ms": 5.0}
    )
    assert finish.kind is EmissionKind.RUN_FINISH
    assert finish.attrs["status"] == "failed"

    suspended = Emission.from_event({"type": "span", "name": "run.suspended"})
    assert suspended.kind is EmissionKind.RUN_FINISH
    assert suspended.attrs["status"] == "suspended"


def test_legacy_sink_write_lifts_to_sink() -> None:
    em = Emission.from_event(
        {"type": "sink.write", "sink": "github_pr", "node_id": "n1", "output_id": "o1"}
    )
    assert em.kind is EmissionKind.SINK
    assert em.attrs["target"] == "github_pr"
    assert em.attrs["committed"] is True
    assert em.node_id == "n1"


def test_legacy_compaction_lifts_to_compaction() -> None:
    em = Emission.from_event(
        {"event": "context.compaction", "strategy": "summarize", "reclaimed_tokens": 100}
    )
    assert em.kind is EmissionKind.COMPACTION
    assert em.attrs["strategy"] == "summarize"


def test_legacy_observer_event_lifts_to_observer() -> None:
    em = Emission.from_event(
        {
            "pipeline": "triage-bot",
            "kind": "cost.spike",
            "severity": "warn",
            "detail": "$2.10 in 5m",
            "run_id": "r9",
        }
    )
    assert em.kind is EmissionKind.OBSERVER
    assert em.attrs["kind"] == "cost.spike"
    assert em.attrs["severity"] == "warn"
    assert em.run_id == "r9"


def test_unknown_legacy_dict_lifts_without_raising() -> None:
    em = Emission.from_event({"weird": "shape", "no": "known keys", "run_id": "rX"})
    assert em.kind is EmissionKind.METRIC
    assert em.attrs["metric"] == "legacy_event"
    assert em.attrs["raw"] == {"weird": "shape", "no": "known keys", "run_id": "rX"}
    assert em.run_id == "rX"


def test_missing_schema_version_defaults_to_current() -> None:
    # A typed-shaped dict that lost its schema_version still lifts (legacy path).
    em = Emission.from_event({"kind": "run_start", "run_id": "r1", "attrs": {"runtime": "x"}})
    assert em.schema_version == EMISSION_SCHEMA_VERSION
    assert em.kind is EmissionKind.RUN_START


# -- read_emissions over a mixed ledger ---------------------------------------
def test_read_emissions_over_mixed_legacy_and_new_ledger() -> None:
    store = SqliteStore()
    run_id = "mixed"
    # legacy loose dicts
    store.append_event(run_id, {"type": "span", "name": "run.start"})
    store.append_event(
        run_id, {"event": "runtime.run", "model": "claude", "cost_usd": 0.01, "runtime": "mock"}
    )
    # a new typed emission
    emit(
        store,
        Emission(kind=EmissionKind.RUN_FINISH, run_id=run_id, attrs={"status": "done"}, ts=1.0),
    )

    out = read_emissions(store, run_id)
    kinds = [e.kind for e in out]
    assert kinds == [EmissionKind.RUN_START, EmissionKind.MODEL, EmissionKind.RUN_FINISH]


# -- taint propagation --------------------------------------------------------
def test_taint_survives_round_trip() -> None:
    em = Emission(kind=EmissionKind.TOOL, run_id="r1", attrs={"tool": "fetch"}, tainted=True)
    assert Emission.from_event(em.to_event()).tainted is True


def test_taint_propagates_from_output_through_run(tmp_path: Path) -> None:
    # A fluid input taints the produced Output; the RUN_FINISH emission inherits it.
    from crawfish.definition import Definition
    from crawfish.run import Run
    from crawfish.runtime import MockRuntime

    dest = tmp_path / "full"
    shutil.copytree(Path(__file__).parent / "fixtures" / "full", dest)
    d = Definition.from_package(str(dest))

    store = SqliteStore()
    ctx = RunContext(store=store)  # type: ignore[arg-type]
    # pr_body is fluid (untrusted) → the Output is tainted.
    run = Run(d, {"repo": "acme/app", "pr_body": "untrusted text"})
    out = asyncio.run(run.execute(ctx, MockRuntime()))
    assert out.tainted is True

    finishes = [e for e in read_emissions(store, ctx.run_id) if e.kind is EmissionKind.RUN_FINISH]
    assert finishes and finishes[-1].tainted is True


# -- volume cap (DoS guard) ---------------------------------------------------
def test_volume_cap_drops_after_limit() -> None:
    store = SqliteStore()
    run_id = "flood"
    for i in range(5):
        emit(
            store,
            Emission(kind=EmissionKind.METRIC, run_id=run_id, attrs={"metric": "m", "value": i}),
            max_per_run=3,
        )
    out = read_emissions(store, run_id)
    # 3 real emissions + exactly one capped-warning emission (written once).
    metrics = [e for e in out if e.kind is EmissionKind.METRIC and e.attrs.get("metric") == "m"]
    capped = [
        e
        for e in out
        if e.kind is EmissionKind.OBSERVER and e.attrs.get("kind") == "emission.capped"
    ]
    assert len(metrics) == 3
    assert len(capped) == 1


def test_no_cap_when_max_per_run_none() -> None:
    store = SqliteStore()
    run_id = "uncapped"
    for i in range(10):
        emit(
            store,
            Emission(kind=EmissionKind.METRIC, run_id=run_id, attrs={"metric": "m", "value": i}),
        )
    assert len(read_emissions(store, run_id)) == 10


# -- security: a secret value never lands in the ledger -----------------------
def test_secret_never_in_ledger_through_scrubbing_store() -> None:
    secret = "hunter2-super-secret"
    inner = SqliteStore()
    store = ScrubbingStore(inner, secrets=[secret])
    run_id = "secure"

    # An emission that (incorrectly) carried a secret value in attrs is still scrubbed
    # on write by ScrubbingStore — the emit path never bypasses redaction.
    emit(
        store,
        Emission(
            kind=EmissionKind.SECRET_LEASE,
            run_id=run_id,
            attrs={"ref": "GITHUB_TOKEN", "node_id": "n1", "leaked": secret},
        ),
    )

    # Read the RAW inner ledger: the secret value must not appear anywhere.
    raw = inner.events(run_id)
    assert secret not in repr(raw)
    # And the proper ref-only field is intact.
    em = read_emissions(store, run_id)[0]
    assert em.attrs["ref"] == "GITHUB_TOKEN"
