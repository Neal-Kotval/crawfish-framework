"""``craw manage`` — see & control running pipelines (CRA-152).

A single view over every deployed pipeline, joining three Store-backed sources: the
**deploy registry** (CRA-151, name/pid/session/schedule), the **execution ledger**
(CRA-134, run state), and the **run-info surface** (CRA-154, last run / cost today).
Control verbs (``stop`` / ``restart`` / ``logs``) act through the same registry.

Dead-process detection runs on every read: a registry row whose PID is gone is
reported ``dead`` so the operator sees reality, not a stale ``running``.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from crawfish.deploy import DeployRegistry, DeployStatus
from crawfish.observe import ObserverSurface, RunInfo

if TYPE_CHECKING:
    from crawfish.deploy import Spawner
    from crawfish.store.base import Store

__all__ = [
    "PipelineStatus",
    "manage_list",
    "restart_target",
    "format_table",
    "workflow_diagram",
    "stats_lines",
    "recent_messages",
    "run_feed",
    "interactive_manage",
    "global_index_path",
    "register_deployment",
    "read_deployments",
    "resolve_deployment_dir",
    "store_for_dir",
    "global_manage_list",
]


class PipelineStatus(BaseModel):
    """A row in ``craw manage``: a deployed pipeline joined with its run state."""

    name: str
    status: str  # running | stopped | dead
    pid: int
    dir: str = ""
    schedule: str | None = None
    uptime_s: float = 0.0
    last_run_status: str | None = None
    last_run_ago_s: float | None = None
    next_fire: str | None = None
    cost_today_usd: float = 0.0
    log_path: str = ""
    runs: list[RunInfo] = Field(default_factory=list)


def _today_cost(infos: list[RunInfo], *, today: datetime) -> float:
    day = today.date()
    return sum(
        ri.cost_usd for ri in infos if datetime.fromtimestamp(ri.started_at, UTC).date() == day
    )


def manage_list(
    store: Store, *, org_id: str = "local", now: datetime | None = None
) -> list[PipelineStatus]:
    """Build the management view for every deployed pipeline.

    Reconciles liveness first (marks dead PIDs), then joins each registry entry with
    its run-info history for uptime, last run, next fire, and today's spend.
    """
    now = now or datetime.now(UTC)
    registry = DeployRegistry(store, org_id=org_id)
    registry.reconcile_liveness()
    surface = ObserverSurface(store, org_id=org_id)

    rows: list[PipelineStatus] = []
    for entry in registry.entries():
        infos = surface.run_info(entry.name)  # newest first
        last = infos[0] if infos else None
        next_fire: str | None = None
        if entry.schedule and entry.status == DeployStatus.RUNNING:
            from crawfish.triggers import parse_schedule

            try:
                next_fire = parse_schedule(entry.schedule).next_after(now).strftime("%H:%M")
            except ValueError:
                next_fire = None
        rows.append(
            PipelineStatus(
                name=entry.name,
                status=entry.status.value,
                pid=entry.pid,
                dir=entry.dir,
                schedule=entry.schedule,
                uptime_s=max(0.0, now.timestamp() - entry.started_at),
                last_run_status=last.status if last else None,
                last_run_ago_s=(now.timestamp() - last.started_at) if last else None,
                next_fire=next_fire,
                cost_today_usd=_today_cost(infos, today=now),
                log_path=entry.log_path,
                runs=infos,
            )
        )
    return rows


def restart_target(
    name: str,
    *,
    store: Store,
    org_id: str = "local",
    spawn: Spawner | None = None,
) -> bool:
    """Stop then re-deploy ``name`` with its recorded dir + schedule. Returns success."""
    from crawfish.deploy import deploy, stop

    registry = DeployRegistry(store, org_id=org_id)
    entry = registry.get(name)
    if entry is None:
        return False
    stop(name, store=store, org_id=org_id)
    deploy(
        entry.dir,
        name=name,
        store=store,
        schedule=entry.schedule,
        backend=entry.backend,
        spawn=spawn,
        org_id=org_id,
    )
    return True


def _fmt_age(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    if seconds < 60:
        return f"{int(seconds)}s ago"
    if seconds < 3600:
        return f"{int(seconds // 60)}m ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h{int((seconds % 3600) // 60)}m ago"
    return f"{int(seconds // 86400)}d ago"


def _fmt_uptime(seconds: float) -> str:
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h{int((seconds % 3600) // 60)}m"
    return f"{int(seconds // 86400)}d"


def format_table(rows: list[PipelineStatus], *, show_dir: bool = False) -> str:
    """Render the management view as a fixed-width table (``craw manage``).

    ``show_dir`` appends a DIR column — useful for the global view, where pipelines come
    from different project directories.
    """
    if not rows:
        return "no deployed pipelines (use `craw deploy`)"
    header = f"{'NAME':<14}{'STATUS':<9}{'UPTIME':<8}{'LAST RUN':<12}{'NEXT':<8}{'$TODAY':>7}"
    if show_dir:
        header += "  DIR"
    lines = [header]
    for r in rows:
        last = f"{r.last_run_status or '—'} {_fmt_age(r.last_run_ago_s)}".strip()
        line = (
            f"{r.name:<14}{r.status:<9}{_fmt_uptime(r.uptime_s):<8}"
            f"{last:<12}{r.next_fire or '—':<8}{f'${r.cost_today_usd:.2f}':>7}"
        )
        if show_dir:
            line += f"  {_shorten_dir(r.dir)}"
        lines.append(line)
    return "\n".join(lines)


def _shorten_dir(path: str) -> str:
    """Abbreviate a project dir with ``~`` for the home prefix, for compact display."""
    if not path:
        return "—"
    home = str(Path.home())
    return "~" + path[len(home) :] if path.startswith(home) else path


# --------------------------------------------------------------------------- detail


def _pipeline_steps(row: PipelineStatus) -> list[tuple[str, str]]:
    """Best-effort ``[(kind, name)]`` for a deployed pipeline's Workflow.

    Discovers the project's Workflow from its dir (the same import the supervisor uses).
    Returns ``[]`` when the shape can't be recovered, so the caller can show a fallback.
    """
    if not row.dir:
        return []
    try:
        from crawfish.deploy import load_workflow

        wf = load_workflow(row.dir)
        if wf is None:
            return []
        steps: list[tuple[str, str]] = []
        for step in wf.steps:  # type: ignore[attr-defined]
            kind = getattr(getattr(step, "kind", None), "value", "") or "node"
            steps.append((str(kind), str(getattr(step, "name", ""))))
        return steps
    except Exception:  # noqa: BLE001 — detail rendering must never crash the TUI
        return []


def workflow_diagram(steps: list[tuple[str, str]]) -> str:
    """Render an ASCII pipeline of ``[(kind, name)]`` as boxes joined by arrows.

    Pure: identical input → identical output, no I/O. ``craw manage``'s detail view
    draws this for the selected pipeline.
    """
    if not steps:
        return "(pipeline shape unavailable)"
    tops, kinds, names, bots = [], [], [], []
    for kind, name in steps:
        label, sub = kind.upper(), name or ""
        width = max(len(label), len(sub), 8) + 2
        tops.append("+" + "-" * width + "+")
        kinds.append("|" + label.center(width) + "|")
        names.append("|" + sub.center(width) + "|")
        bots.append("+" + "-" * width + "+")
    return "\n".join(
        [
            "     ".join(tops),
            " --> ".join(kinds),
            "     ".join(names),
            "     ".join(bots),
        ]
    )


def stats_lines(row: PipelineStatus, report: object | None = None) -> list[str]:
    """Human-readable stat lines for a pipeline's detail panel (pure)."""
    last = row.runs[0] if row.runs else None
    lines = [
        f"status:     {row.status}   pid {row.pid}",
        f"uptime:     {_fmt_uptime(row.uptime_s)}   runs: {len(row.runs)}",
        f"last run:   {(row.last_run_status or '—')} {_fmt_age(row.last_run_ago_s)}".rstrip(),
        f"cost today: ${row.cost_today_usd:.4f}",
    ]
    if row.schedule:
        lines.append(f"schedule:   {row.schedule}   next: {row.next_fire or '—'}")
    if last is not None:
        lines.append(f"items:      {last.items}   backend: {last.backend}   ver {last.version}")
    if report is not None:
        cost = getattr(report, "cost_usd", 0.0)
        latency = getattr(report, "latency_ms", None)
        events = getattr(report, "event_count", 0)
        lat = f"{latency:.0f}ms" if isinstance(latency, (int, float)) else "—"
        lines.append(f"last cost:  ${cost:.4f}   latency: {lat}   events: {events}")
    return lines


def recent_messages(report: object | None, n: int = 5) -> list[str]:
    """The last ``n`` transcript entries of a run, formatted ``[kind] text`` (pure)."""
    if report is None:
        return ["(no run yet)"]
    transcript = list(getattr(report, "transcript", []) or [])
    if not transcript:
        return ["(no transcript events)"]
    out: list[str] = []
    for entry in transcript[-n:]:
        body = getattr(entry, "text", "") or getattr(entry, "detail", "") or ""
        kind = getattr(entry, "kind", "?")
        out.append(f"[{kind}] {body}".rstrip())
    return out


def run_feed(runs: list[RunInfo], n: int = 5) -> list[str]:
    """The last ``n`` runs as a one-line-each feed: ``status · $cost · age`` (pure).

    A pipeline-level fallback for the detail view when a run has no agent transcript
    (e.g. mock/dry-run cycles) — the cycle history is always informative.
    """
    if not runs:
        return ["(no runs yet)"]
    now = datetime.now(UTC).timestamp()
    out: list[str] = []
    for ri in runs[:n]:
        ago = _fmt_age(now - ri.started_at)
        out.append(f"{ri.status:<7} ${ri.cost_usd:.4f}  {ago}  ({ri.run_id[:8]})")
    return out


# ----------------------------------------------------------------- global registry index
#
# Deploys are stored per-project (run data lives in `<dir>/.crawfish/crawfish.db`). A tiny
# global index at `~/.crawfish/deployments.json` maps each pipeline name -> project dir, so
# `craw manage` from anywhere can aggregate every deployment without centralising run data.


def global_index_path() -> Path:
    """Path of the global deployments index (honours ``$CRAWFISH_HOME`` for tests)."""
    home = os.environ.get("CRAWFISH_HOME") or str(Path.home() / ".crawfish")
    return Path(home) / "deployments.json"


def read_deployments(*, path: Path | None = None) -> list[dict[str, str]]:
    """Read the global index as ``[{"name", "dir"}, ...]`` (empty/corrupt → ``[]``)."""
    p = path or global_index_path()
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text())
    except (ValueError, OSError):
        return []
    return [e for e in data if isinstance(e, dict) and "name" in e and "dir" in e]


def register_deployment(name: str, project_dir: str, *, path: Path | None = None) -> None:
    """Record (or update) ``name -> dir`` in the global index (idempotent by name)."""
    p = path or global_index_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    by_name = {e["name"]: e for e in read_deployments(path=p)}
    by_name[name] = {"name": name, "dir": str(Path(project_dir).resolve())}
    ordered = sorted(by_name.values(), key=lambda e: e["name"])
    p.write_text(json.dumps(ordered, indent=2))


def resolve_deployment_dir(name: str, *, path: Path | None = None) -> str | None:
    """The project dir a globally-registered pipeline ``name`` was deployed from."""
    for entry in read_deployments(path=path):
        if entry["name"] == name:
            return entry["dir"]
    return None


def store_for_dir(project_dir: str) -> Store:
    """Open the per-project Store that holds a deployment's registry + run data."""
    from crawfish.store import SqliteStore

    return SqliteStore(Path(project_dir) / ".crawfish" / "crawfish.db")


def global_manage_list(
    *,
    open_store: Callable[[str], Store] | None = None,
    path: Path | None = None,
    now: datetime | None = None,
) -> list[PipelineStatus]:
    """Aggregate management rows across every globally-registered deployment.

    Reads the global index, opens each project's own Store, and concatenates its
    ``manage_list`` rows (so run data stays per-project). A dir whose store can't be read
    is skipped rather than failing the whole view.
    """
    opener = open_store or store_for_dir
    rows: list[PipelineStatus] = []
    seen: set[str] = set()
    for entry in read_deployments(path=path):
        project_dir = entry["dir"]
        if project_dir in seen:
            continue
        seen.add(project_dir)
        try:
            rows.extend(manage_list(opener(project_dir), now=now))
        except Exception:  # noqa: BLE001 — one broken project must not blank the whole view
            continue
    return sorted(rows, key=lambda r: r.name)


def interactive_manage(
    rows_provider: Callable[[], list[PipelineStatus]], *, org_id: str = "local"
) -> int:
    """Curses TUI for ``craw manage``: scroll pipelines, open one, stop it.

    Driven by ``rows_provider`` (called each refresh) — so it serves both the scoped and
    global views uniformly. Actions resolve each pipeline's own Store from its ``row.dir``,
    so stop/restart/inspect hit the right project regardless of which view you're in.

    List view: Up/Down (or j/k) to move, Enter/Right to open, s to stop, r to restart,
    q to quit. Detail view: ASCII pipeline + stats + recent transcript; x stops,
    r restarts, Esc/Left/q goes back. The screen refreshes on a timer so uptime/age and
    fresh runs appear without a keypress. Interactive-only; not unit-tested (the pure
    render helpers above are).
    """
    import curses

    from crawfish.deploy import stop
    from crawfish.inspector import inspect_run

    def _stop(row: PipelineStatus) -> None:
        if row.dir:
            stop(row.name, store=store_for_dir(row.dir), org_id=org_id)

    def _restart(row: PipelineStatus) -> None:
        if row.dir:
            restart_target(row.name, store=store_for_dir(row.dir), org_id=org_id)

    def _loop(stdscr: curses.window) -> int:  # pragma: no cover - interactive
        curses.curs_set(0)
        stdscr.timeout(700)  # ms; getch returns -1 on timeout so we redraw periodically
        selected = 0
        detail = False

        def put(y: int, x: int, text: str, attr: int = 0) -> None:
            max_y, max_x = stdscr.getmaxyx()
            if 0 <= y < max_y:
                try:
                    stdscr.addnstr(y, x, text, max(0, max_x - x - 1), attr)
                except curses.error:
                    pass

        while True:
            rows = rows_provider()
            if rows:
                selected = max(0, min(selected, len(rows) - 1))
            stdscr.erase()

            if not detail or not rows:
                put(0, 0, "craw manage — deployed pipelines", curses.A_BOLD)
                put(1, 0, "up/down select · enter open · s stop · r restart · q quit", curses.A_DIM)
                header = (
                    f"{'NAME':<16}{'STATUS':<9}{'UPTIME':<8}"
                    f"{'LAST RUN':<13}{'NEXT':<7}{'$TODAY':>8}"
                )
                put(3, 0, header, curses.A_UNDERLINE)
                if not rows:
                    put(5, 2, "no deployed pipelines — `craw deploy <dir>`")
                for i, r in enumerate(rows):
                    last = f"{r.last_run_status or '—'} {_fmt_age(r.last_run_ago_s)}".strip()
                    line = (
                        f"{r.name:<16}{r.status:<9}{_fmt_uptime(r.uptime_s):<8}"
                        f"{last:<13}{r.next_fire or '—':<7}{f'${r.cost_today_usd:.2f}':>8}"
                    )
                    put(4 + i, 0, line, curses.A_REVERSE if i == selected else 0)
            else:
                row = rows[selected]
                report = None
                if row.runs and row.dir:
                    try:
                        report = inspect_run(
                            store_for_dir(row.dir), row.runs[0].run_id, org_id=org_id
                        )
                    except Exception:  # noqa: BLE001
                        report = None
                put(0, 0, f"craw manage — {row.name}", curses.A_BOLD)
                put(1, 0, "x stop · r restart · Esc back · q quit", curses.A_DIM)
                y = 3
                for line in workflow_diagram(_pipeline_steps(row)).splitlines():
                    put(y, 2, line)
                    y += 1
                y += 1
                put(y, 0, "stats", curses.A_UNDERLINE)
                y += 1
                for line in stats_lines(row, report):
                    put(y, 2, line)
                    y += 1
                y += 1
                put(y, 0, "recent activity", curses.A_UNDERLINE)
                y += 1
                has_transcript = report is not None and getattr(report, "transcript", None)
                activity = recent_messages(report, 5) if has_transcript else run_feed(row.runs, 5)
                for line in activity:
                    put(y, 2, line)
                    y += 1

            stdscr.refresh()
            ch = stdscr.getch()
            if ch == -1:
                continue
            if ch in (ord("q"), ord("Q")):
                return 0
            if not detail:
                if ch in (curses.KEY_DOWN, ord("j")) and rows:
                    selected = min(len(rows) - 1, selected + 1)
                elif ch in (curses.KEY_UP, ord("k")) and rows:
                    selected = max(0, selected - 1)
                elif ch in (curses.KEY_ENTER, 10, 13, curses.KEY_RIGHT, ord("l")) and rows:
                    detail = True
                elif ch == ord("s") and rows:
                    _stop(rows[selected])
                elif ch == ord("r") and rows:
                    _restart(rows[selected])
            else:
                if ch in (27, curses.KEY_LEFT, ord("h")):  # 27 == Esc
                    detail = False
                elif ch in (ord("x"), ord("s")) and rows:
                    _stop(rows[selected])
                elif ch == ord("r") and rows:
                    _restart(rows[selected])

    return curses.wrapper(_loop)
