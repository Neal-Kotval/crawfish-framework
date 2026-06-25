"""``craw code diagnose <run_id>`` — why a run failed / abstained / escalated (UNFILED-DIAGNOSE).

RFC §12.4 (Medium): debug a failed run from the ledger. The inputs all exist — the
:class:`~crawfish.observe.RunInfo`, the DLQ (``dead_letter`` records), the observer events
(:meth:`ObserverSurface.events`), and the failing node IO (the emission stream). This verb
**correlates** them into a structured root cause and points at ``craw replay --swap`` to test a
fix for near-$0 (every unaffected leaf replays bit-for-bit; only the dirtied fraction re-runs).

**Read-only + scrubbed, by construction.** It reads through the *same* seam the dashboard uses
(:func:`crawfish.code.dashboard.build_data` → a :class:`~crawfish.secrets.ScrubbingStore`-wrapped
surface), so a secret/PII is redacted before it could reach the diagnosis, and diagnose never
drains/deletes a DLQ entry or mutates state. The failing-IO ``detail`` is fluid/tainted and is
**output-encoded** through the dashboard's ``encode_field`` chokepoint before it lands in the
record, so an injected payload in a failing item cannot ride the diagnosis into a renderer or
back into the agent's instruction stream. The remediation only *suggests* ``replay --swap``
(eval-mode, near-$0); it fires nothing.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from crawfish.code import (
    EXIT_EXPECTED_FAILURE,
    EXIT_OK,
    SCHEMA_VERSIONS,
    ErrorCode,
    emit_error,
    emit_json,
)

if TYPE_CHECKING:
    from crawfish.code.dashboard.data import DashboardData

# Self-registering schema version (CRA-269; ``setdefault`` keeps this additive, no shared edit).
SCHEMA_VERSIONS.setdefault("code.diagnose", (1, 0))  # type: ignore[attr-defined]

DIAGNOSE_SCHEMA = "craw.code.diagnose.v1"


class FirstFailure(BaseModel):
    """The first failing node of a run + its error class and the implicated input item.

    ``node`` is a static identifier; ``detail`` is fluid/tainted and is carried output-encoded.
    ``error_class`` is one of the stable buckets :func:`_classify_error` maps onto.
    """

    node: str = ""
    error_class: str = "unknown"  # timeout | budget | validation | sink_gate | unknown
    item_id: str = ""
    detail: str = ""


class Remediation(BaseModel):
    """The concrete near-$0 ``craw replay --swap`` move that tests a fix for this run."""

    action: str = "replay_swap"
    command: str = ""
    estimated_usd: float = 0.0


class Diagnosis(BaseModel):
    """The structured root-cause record (the ``craw.code.diagnose.v1`` body)."""

    run_id: str
    pipeline: str = ""
    status: str = "unknown"
    first_failure: FirstFailure = Field(default_factory=FirstFailure)
    dlq: list[dict[str, object]] = Field(default_factory=list)
    observer_events: list[str] = Field(default_factory=list)
    remediation: Remediation = Field(default_factory=Remediation)


#: Stable error-class buckets, matched against the DLQ ``error`` string / observer kind. Keyed on
#: static substrings only (never on fluid free-text the attacker controls beyond these markers).
_ERROR_CLASS_MARKERS: tuple[tuple[str, str], ...] = (
    ("timeout", "timeout"),
    ("budget", "budget"),
    ("cancel", "budget"),
    ("sink", "sink_gate"),
    ("static", "sink_gate"),
    ("fluid", "sink_gate"),
    ("schema", "validation"),
    ("validation", "validation"),
    ("invalid", "validation"),
)


def _classify_error(text: str) -> str:
    """Map an error string to a stable error-class bucket (deterministic, substring-keyed)."""
    low = text.lower()
    for marker, klass in _ERROR_CLASS_MARKERS:
        if marker in low:
            return klass
    return "unknown"


def diagnose_run(data: DashboardData, run_id: str) -> Diagnosis | None:
    """Correlate RunInfo + observer events + DLQ + emissions for ``run_id`` into a root cause.

    Read-only over the scrubbed surface. Returns ``None`` if the run is unknown (no RunInfo).
    The first failure is taken from the run's DLQ entry (item id + error → class) when present,
    else from a ``failure.``/``quality.`` observer event; ``detail`` is output-encoded. The
    remediation is the exact ``craw replay --swap`` command for the run at a $0 estimate.
    """
    from crawfish.code.dashboard.encoding import Encoding, encode_field

    info = data._surface.get_run_info(run_id)  # noqa: SLF001 — scrubbed surface read
    if info is None:
        return None

    # -- observer events for this run (filtered out of the pipeline's stream) ----------------
    events = [e for e in data._surface.events(info.pipeline) if e.run_id == run_id]  # noqa: SLF001
    observer_kinds = [e.kind for e in events]  # static identifiers

    # -- DLQ entries for the run's batch (read-only; the batch id is the run id) -------------
    dlq_raw = _dead_letters_for(data, run_id)
    dlq: list[dict[str, object]] = [
        {
            "item_id": str(r.get("item_id") or ""),
            "reason": encode_field(r.get("error") or "", Encoding.HTML_BODY),
        }
        for r in dlq_raw
    ]

    # -- first failure: prefer a DLQ entry, else the first failure/quality observer event ----
    first = FirstFailure()
    if dlq_raw:
        entry = dlq_raw[0]
        err = str(entry.get("error") or "")
        first = FirstFailure(
            node=_first_failed_node(data, run_id),
            error_class=_classify_error(err),
            item_id=str(entry.get("item_id") or ""),
            detail=encode_field(err, Encoding.HTML_BODY),
        )
    else:
        ev = next((e for e in events if e.kind.startswith(("failure.", "quality."))), None)
        if ev is not None:
            first = FirstFailure(
                node=_first_failed_node(data, run_id),
                error_class=_classify_error(ev.kind + " " + ev.detail),
                detail=encode_field(ev.detail, Encoding.HTML_BODY),
            )

    return Diagnosis(
        run_id=run_id,
        pipeline=info.pipeline,
        status=info.status,
        first_failure=first,
        dlq=dlq,
        observer_events=observer_kinds,
        remediation=Remediation(
            action="replay_swap",
            command=f"craw replay --swap model=<candidate-model> {run_id}",
            estimated_usd=0.0,
        ),
    )


def _first_failed_node(data: DashboardData, run_id: str) -> str:
    """The node id of the first non-clean emission for a run (static identifier), or ``""``.

    Reads the run's emission stream (read-only) and returns the first ``SINK``/``TOOL``/
    ``JAIL_VIOLATION``/``OBSERVER`` emission's ``node_id`` — a stable identifier, never fluid.
    """
    from crawfish.emission import EmissionKind, read_emissions

    interesting = {
        EmissionKind.SINK,
        EmissionKind.TOOL,
        EmissionKind.JAIL_VIOLATION,
        EmissionKind.OBSERVER,
    }
    for em in read_emissions(data._store, run_id, org_id=data._org):  # noqa: SLF001
        if em.kind in interesting and em.node_id:
            return em.node_id
    return ""


def _dead_letters_for(data: DashboardData, run_id: str) -> list[dict[str, object]]:
    """The DLQ records for a run's batch (read-only; ``batch_id`` is the run id).

    Reads the ``dead_letter`` record kind through the Store protocol (``list_records``) — never
    drains/deletes an entry. The store is the dashboard's already-scrubbed wrapper.
    """
    store = data._store  # noqa: SLF001
    return [
        dict(r)
        for r in store.list_records("dead_letter", org_id=data._org)  # noqa: SLF001
        if str(r.get("batch_id") or "") == run_id
    ]


def register(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    """Register ``craw code diagnose`` (self-registering; one owner)."""
    from crawfish.code.cli import add_common_args

    p = subparsers.add_parser(
        "diagnose",
        help="why a run failed/abstained/escalated from the ledger (read-only, scrubbed)",
    )
    p.add_argument("run_id", help="the run id to diagnose")
    p.add_argument(
        "--project", default=".", help="project directory holding .crawfish/ (default: cwd)"
    )
    add_common_args(p)
    p.set_defaults(func=_cmd_diagnose)


def _cmd_diagnose(args: argparse.Namespace) -> int:
    """``craw code diagnose <run_id> [--project] [--org] [--json]`` — emit the root-cause record."""
    from crawfish.code.dashboard import build_data

    org = getattr(args, "org", "local")
    as_json = getattr(args, "as_json", False)
    project = Path(getattr(args, "project", "."))
    if not (project / ".crawfish").is_dir():
        return emit_error(
            ErrorCode.NOT_FOUND,
            remediation=(
                "No .crawfish/ ledger was found; run the pipeline (or `craw code init`) first, "
                "or pass --project to point at the project directory."
            ),
            detail={"project": str(project)},
            as_json=as_json,
        )
    data = build_data(project, org_id=org)
    try:
        diagnosis = diagnose_run(data, args.run_id)
        if diagnosis is None:
            return emit_error(
                ErrorCode.NOT_FOUND,
                remediation=f"No run {args.run_id!r} was found in the ledger for org {org!r}.",
                detail={"run_id": args.run_id},
                as_json=as_json,
            )
        body = diagnosis.model_dump()
        if as_json:
            emit_json("code.diagnose", body, org=org)
        else:
            _print_diagnosis(diagnosis)
        return EXIT_OK
    finally:
        store = getattr(data, "_store", None)
        close = getattr(store, "close", None)
        if callable(close):
            close()


def _print_diagnosis(d: Diagnosis) -> None:
    """Human rendering of the root-cause record."""
    print(f"craw code diagnose {d.run_id} — {d.pipeline} [{d.status}]")
    ff = d.first_failure
    if ff.node or ff.error_class != "unknown":
        print(f"  first failure: node={ff.node!r} class={ff.error_class} item={ff.item_id!r}")
    if d.dlq:
        print(f"  dlq: {len(d.dlq)} item(s)")
    if d.observer_events:
        print(f"  observer: {', '.join(d.observer_events)}")
    print(f"  remediation: {d.remediation.command} (≈${d.remediation.estimated_usd})")


# ``EXIT_EXPECTED_FAILURE`` is the spec's exit-1 "no such run"; we surface it through the
# ``not_found`` envelope above (which already maps to exit 2 in the closed CRA-243 table). Keep
# the import bound so a future reorganization that wants the granular code has it to hand.
_ = EXIT_EXPECTED_FAILURE
