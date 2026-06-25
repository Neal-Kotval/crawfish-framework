"""``craw code review`` — the ledger → authoring digest (UNFILED-REVIEW).

RFC §12.4 (Urgent) "close the self-generating loop": fold the observer/run ledger and the
DLQ over a ``--since`` window into a ranked, agent-readable digest where **each finding
carries a suggested next authoring action** — the loop's closing edge. The dashboard's
human-facing view becomes an agent-actionable feedback signal.

**Read-only + scrubbed, by construction.** Every read goes through the *same* seam the
dashboard uses (:func:`crawfish.code.dashboard.build_data`): the project Store wrapped in a
:class:`~crawfish.secrets.ScrubbingStore`, surfaced via :class:`~crawfish.observe.ObserverSurface`
— so a secret/PII is redacted before it could ever reach the digest, and ``review`` never
mutates state (it triggers no run, drains no DLQ entry). Tainted ``detail`` is fluid/untrusted
markup: it is **output-encoded** through the dashboard's ``encode_field`` chokepoint before it
lands in the digest, so an injected ``<script>`` in a ticket body cannot ride the digest into a
downstream renderer (or back into the agent's instruction stream as an instruction).

The ``suggested_action`` is a *deterministic* mapping from a finding's **static** ``kind`` to a
concrete ``craw code`` move (never derived from fluid ``detail``). It is only a *suggestion*: the
agent still runs any consequential ``--live`` move through the HITL gate (:mod:`crawfish.code.gate`)
before acting.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel

from crawfish.code import (
    EXIT_OK,
    SCHEMA_VERSIONS,
    ErrorCode,
    emit_error,
    emit_json,
)

if TYPE_CHECKING:
    from crawfish.code.dashboard.data import DashboardData

# Self-registering schema version (CRA-269; ``setdefault`` keeps this additive, no shared edit).
SCHEMA_VERSIONS.setdefault("code.review", (1, 0))  # type: ignore[attr-defined]

REVIEW_SCHEMA = "craw.code.review.v1"

#: Severity rank for ordering (higher = louder). Findings are ranked by severity then recency.
_SEVERITY_RANK = {"critical": 2, "warn": 1, "info": 0}


class Finding(BaseModel):
    """One ranked digest finding with its deterministic suggested next authoring action.

    ``kind``/``severity``/``pipeline`` are stable static identifiers (safe to render/sort on);
    ``detail`` is fluid/tainted and is carried **output-encoded** (the digest is the chokepoint).
    ``suggested_action`` is mapped from the static ``kind`` only — never from fluid ``detail``.
    """

    severity: str = "info"
    kind: str
    pipeline: str = ""
    run_id: str | None = None
    detail: str = ""
    dlq_count: int = 0
    suggested_action: str = ""


def _suggest(kind: str, *, pipeline: str, run_id: str | None) -> str:
    """Map a finding's **static** ``kind`` to a concrete next ``craw code`` move (deterministic).

    The mapping is over the stable, static ``kind`` identifier only (a fluid ``detail`` never
    chooses the action): a cost spike → tune for cost; a quality flag → refine; a failure-rate
    spike / DLQ entries → diagnose the run; a baseline regression → review the last propose.
    """
    component = f"definitions/{pipeline}" if pipeline else "<component>"
    if kind.startswith("cost."):
        return f"craw code optimize {component} --mode tune"
    if kind.startswith("quality."):
        return f"craw code optimize {component} --mode refine"
    if kind.startswith("failure."):
        return f"craw code diagnose {run_id}" if run_id else "craw code diagnose <run_id>"
    if kind.startswith("regression"):
        return f"review the last `craw code propose {component}`; consider `craw code reject`"
    return f"open {component}"


def build_digest(data: DashboardData, *, since: str = "-1d") -> list[Finding]:
    """Fold observer events + run-info + DLQ entries over ``since`` into ranked findings.

    Read-only over the scrubbed surface. Observer events become findings keyed on their static
    ``kind``; DLQ entries (grouped by their batch) become one ``failure.rate`` finding carrying a
    ``dlq_count``. Each finding's ``detail`` is output-encoded; the suggested action is mapped
    from the static ``kind``. The list is ranked by severity (critical > warn > info) then recency.
    """
    from crawfish.code.dashboard.encoding import Encoding, encode_field

    findings: list[tuple[float, Finding]] = []

    # -- observer events (cost spike / quality flag / failure rate / regression) ------------
    for event in data.events(since=since):
        findings.append(
            (
                event.ts,
                Finding(
                    severity=event.severity.value,
                    kind=event.kind,  # static identifier — safe, unencoded
                    pipeline=event.pipeline,  # static identifier
                    run_id=event.run_id,
                    detail=encode_field(event.detail, Encoding.HTML_BODY),
                    suggested_action=_suggest(
                        event.kind, pipeline=event.pipeline, run_id=event.run_id
                    ),
                ),
            )
        )

    # -- DLQ entries: one failure.rate finding per batch carrying the dead-letter count ------
    for batch_id, entries in _dead_letters_by_batch(data).items():
        run_id = str(entries[0].get("run_id") or batch_id)
        findings.append(
            (
                0.0,  # DLQ rows carry no ts; rank them after same-severity dated events
                Finding(
                    severity="critical",
                    kind="failure.rate",
                    pipeline=str(entries[0].get("pipeline") or ""),
                    run_id=run_id,
                    dlq_count=len(entries),
                    suggested_action=_suggest("failure.rate", pipeline="", run_id=run_id),
                ),
            )
        )

    # Rank by severity (desc) then recency (desc). Stable for a deterministic snapshot.
    findings.sort(key=lambda pair: (_SEVERITY_RANK.get(pair[1].severity, 0), pair[0]), reverse=True)
    return [f for _ts, f in findings]


def _dead_letters_by_batch(data: DashboardData) -> dict[str, list[dict[str, object]]]:
    """Read DLQ records (read-only) off the scrubbed store, grouped by ``batch_id``.

    Reads the ``dead_letter`` record kind through the Store protocol (``list_records``) — never
    drains or deletes an entry (read-only). The store is the dashboard's already-scrubbed wrapper,
    so a secret in a failing payload is redacted before it reaches the digest.
    """
    store = data._store  # noqa: SLF001 — the scrubbed store the facade was built over
    grouped: dict[str, list[dict[str, object]]] = {}
    for rec in store.list_records("dead_letter", org_id=data._org):  # noqa: SLF001
        batch_id = str(rec.get("batch_id") or "")
        grouped.setdefault(batch_id, []).append(dict(rec))
    return grouped


def register(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    """Register ``craw code review`` (self-registering; one owner)."""
    from crawfish.code.cli import add_common_args

    p = subparsers.add_parser(
        "review", help="ledger/observer/DLQ → ranked authoring digest (read-only, scrubbed)"
    )
    p.add_argument(
        "--since", default="-1d", help="window for the digest (e.g. -1d / -6h); default -1d"
    )
    p.add_argument(
        "--project", default=".", help="project directory holding .crawfish/ (default: cwd)"
    )
    add_common_args(p)
    p.set_defaults(func=_cmd_review)


def _cmd_review(args: argparse.Namespace) -> int:
    """``craw code review [--since] [--project] [--org] [--json]`` — emit the authoring digest."""
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
        findings = build_digest(data, since=getattr(args, "since", "-1d"))
        body = {
            "since": getattr(args, "since", "-1d"),
            "findings": [f.model_dump() for f in findings],
        }
        if as_json:
            emit_json("code.review", body, org=org)
        else:
            _print_digest(getattr(args, "since", "-1d"), findings)
        return EXIT_OK
    finally:
        store = getattr(data, "_store", None)
        close = getattr(store, "close", None)
        if callable(close):
            close()


def _print_digest(since: str, findings: list[Finding]) -> None:
    """Human rendering of the digest (the encoded detail + the suggested action per finding)."""
    if not findings:
        print(f"craw code review ({since}): nothing needs attention.")
        return
    print(f"craw code review ({since}): {len(findings)} finding(s)")
    for f in findings:
        head = f"  [{f.severity}] {f.kind} {f.pipeline}".rstrip()
        if f.dlq_count:
            head += f" (dlq={f.dlq_count})"
        print(head)
        print(f"    → {f.suggested_action}")
