"""Run inspector / devtools — CLI-level introspection over the Store (CRA-120).

The trust + DX layer (the React-DevTools analog): let a framework user *see what
happened* on any run, locally, without a live model call. Everything is derived
from the Store's append-only, ordered event ledger — the same spans Run emits
(``run.start`` / ``run.finish`` with ``cost_usd`` / ``latency_ms`` / ``status``)
and the ``runtime.run`` telemetry AgentRuntime appends (``model`` / ``cost_usd``).

Three primitives back the CLI:

* :func:`inspect_run` — a :class:`RunReport` summary (``craw inspect <run>``).
* :func:`tail_events` — events after a sequence index, the poll primitive that
  ``craw logs`` uses for live streaming (the ledger is append-only + ordered).
* :func:`format_report` — a concise human-readable render of a report.

Rich dashboards are Phase 2 (Observability); this stays CLI-shaped.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from crawfish.core.types import JSONValue
from crawfish.store.base import Store

__all__ = [
    "ToolCallRecord",
    "TranscriptEntry",
    "RunReport",
    "inspect_run",
    "tail_events",
    "format_report",
]


class ToolCallRecord(BaseModel):
    """A tool invocation observed in the ledger (name + input + optional result)."""

    name: str
    input: dict[str, JSONValue] = Field(default_factory=dict)
    result: str | None = None


class TranscriptEntry(BaseModel):
    """One ordered line of the run's transcript (text or a tool event)."""

    kind: str  # text | tool_use | tool_result | result | span | runtime.run | ...
    text: str = ""
    detail: str | None = None


class RunReport(BaseModel):
    """A summary of a single run, derived from the Store's event ledger (CRA-120).

    ``found`` is ``False`` for an unknown run (no events) — callers get a clearly
    empty report rather than a crash.
    """

    run_id: str
    found: bool = False
    status: str = "unknown"
    cost_usd: float = 0.0
    latency_ms: float | None = None
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)
    transcript: list[TranscriptEntry] = Field(default_factory=list)
    event_count: int = 0


def _as_str(value: JSONValue | None) -> str:
    return "" if value is None else str(value)


def inspect_run(store: Store, run_id: str, *, org_id: str = "local") -> RunReport:
    """Summarize a run from the Store's event ledger (``craw inspect <run>``).

    Derives status / total cost / latency from the ``span`` events Run emits
    (``run.start`` / ``run.finish``), accumulates cost from ``runtime.run``
    telemetry, and builds an ordered transcript + tool-call list. Performs no
    live model call — pure read over append-only events.
    """
    events = store.events(run_id, org_id=org_id)
    report = RunReport(run_id=run_id, event_count=len(events))
    if not events:
        return report

    report.found = True
    cost_from_finish: float | None = None
    cost_from_runtime = 0.0

    for event in events:
        etype = _as_str(event.get("type"))
        name = _as_str(event.get("name"))
        kind = _as_str(event.get("kind"))
        ekey = _as_str(event.get("event"))

        # -- Run spans (status / cost / latency) ----------------------------
        if etype == "span":
            report.transcript.append(
                TranscriptEntry(kind=f"span:{name}", detail=_as_str(event.get("status")) or None)
            )
            if name == "run.finish":
                status = event.get("status")
                if status is not None:
                    report.status = str(status)
                latency = event.get("latency_ms")
                if isinstance(latency, (int, float)):
                    report.latency_ms = float(latency)
                cost = event.get("cost_usd")
                if isinstance(cost, (int, float)):
                    cost_from_finish = float(cost)
            elif name == "run.suspended":
                report.status = "suspended"
            continue

        # -- Runtime telemetry (model / cost) -------------------------------
        if ekey == "runtime.run":
            cost = event.get("cost_usd")
            if isinstance(cost, (int, float)):
                cost_from_runtime += float(cost)
            model = _as_str(event.get("model"))
            report.transcript.append(TranscriptEntry(kind="runtime.run", detail=model or None))
            continue

        # -- Transcript-shaped runtime events (TEXT/TOOL_USE/...) -----------
        if kind:
            report.transcript.append(TranscriptEntry(kind=kind, text=_as_str(event.get("text"))))
            if kind == "tool_use":
                tool = event.get("tool")
                if isinstance(tool, dict):
                    raw_input = tool.get("input")
                    report.tool_calls.append(
                        ToolCallRecord(
                            name=_as_str(tool.get("name")),
                            input=raw_input if isinstance(raw_input, dict) else {},
                        )
                    )
                elif event.get("name"):
                    report.tool_calls.append(ToolCallRecord(name=_as_str(event.get("name"))))
            continue

    # run.finish cost is authoritative when present; otherwise sum the telemetry.
    report.cost_usd = cost_from_finish if cost_from_finish is not None else cost_from_runtime
    return report


def tail_events(
    store: Store, run_id: str, *, after_seq: int = 0, org_id: str = "local"
) -> list[dict[str, JSONValue]]:
    """Return events after ``after_seq`` — the poll primitive for ``craw logs``.

    The Store's ledger is append-only and ordered, so a caller polls with the
    sequence index of the last event it saw and gets only what is new. ``seq`` is
    a 0-based positional index into the ordered ledger; ``after_seq=0`` skips the
    first event. Pass ``after_seq=-1`` (or any negative value) to get everything.
    """
    events = store.events(run_id, org_id=org_id)
    if after_seq < 0:
        return events
    return events[after_seq:] if after_seq <= len(events) else []


def format_report(report: RunReport) -> str:
    """Render a concise human-readable summary for ``craw inspect`` output."""
    if not report.found:
        return f"run {report.run_id}: not found (no events recorded)"

    lines: list[str] = [f"run {report.run_id}", f"  status: {report.status}"]
    lines.append(f"  cost:   ${report.cost_usd:.4f}")
    if report.latency_ms is not None:
        lines.append(f"  latency: {report.latency_ms:.1f}ms")
    lines.append(f"  events: {report.event_count}")

    if report.tool_calls:
        lines.append("  tool calls:")
        for call in report.tool_calls:
            lines.append(f"    - {call.name}({_render_input(call.input)})")
    else:
        lines.append("  tool calls: none")

    lines.append("  transcript:")
    if report.transcript:
        for entry in report.transcript:
            body = entry.text or entry.detail or ""
            suffix = f": {body}" if body else ""
            lines.append(f"    [{entry.kind}]{suffix}")
    else:
        lines.append("    (empty)")

    return "\n".join(lines)


def _render_input(data: dict[str, JSONValue]) -> str:
    return ", ".join(f"{k}={v!r}" for k, v in data.items())
