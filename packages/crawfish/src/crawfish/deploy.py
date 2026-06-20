"""Always-on local pipeline deployment (CRA-151).

``craw deploy`` launches a project's pipeline as a long-lived, **detached**
supervisor process: it survives the shell closing (``start_new_session``), fires the
pipeline on its **trigger** (cron) or continuously, **auto-restarts** failed cycles,
and **resumes** orphaned runs through the execution ledger on restart. It registers a
PID entry in a Store-backed **deploy registry** so ``craw manage`` / ``craw visualize``
can see and control it.

Security spine: the detached process carries **no secret values** in its argv, its
session name (``crawfish/<pipeline>``), or env dumps — it resolves secrets by
reference exactly like a foreground run, and all run telemetry/observer events flow
through a :class:`~crawfish.secrets.ScrubbingStore`, so the log/ledger never holds a
raw credential.

The supervisor logic (registry, scheduling, one cycle) is separated from the spawn so
it is unit-testable without launching a daemon. See ADR 0009 (daemon vs tmux).
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from crawfish.core.context import RunContext
from crawfish.core.ids import new_id
from crawfish.ledger import ExecState, ExecutionLedger
from crawfish.observe import ObserverEvent, ObserverSurface, RunInfo, Severity
from crawfish.secrets import redact
from crawfish.triggers import parse_schedule

if TYPE_CHECKING:
    from crawfish.store.base import Store

__all__ = [
    "DeployStatus",
    "DeployEntry",
    "DeployRegistry",
    "Supervisor",
    "RunFn",
    "default_run_fn",
    "load_workflow",
    "load_trigger",
    "deploy",
    "stop",
    "supervise_main",
]

_REGISTRY_KIND = "deploy_entry"

# A pipeline cycle: given a fresh RunContext, do one unit of work. Raising marks the
# cycle failed (the supervisor records it and stays alive — that is the auto-restart).
RunFn = Callable[[RunContext], None]


class DeployStatus(str, Enum):
    RUNNING = "running"  # supervisor process alive
    STOPPED = "stopped"  # cleanly stopped
    DEAD = "dead"  # PID no longer alive (crashed without cleanup)


class DeployEntry(BaseModel):
    """A registry row describing one deployed pipeline."""

    name: str
    pid: int
    dir: str
    session: str  # e.g. "crawfish/triage-bot" — never carries a secret
    backend: str = "daemon"  # daemon | tmux
    schedule: str | None = None
    status: DeployStatus = DeployStatus.RUNNING
    started_at: float = Field(default_factory=lambda: datetime.now(UTC).timestamp())
    log_path: str = ""
    version: str = "0.1.0"


def _pid_alive(pid: int) -> bool:
    """True if a process with ``pid`` exists (no signal actually delivered)."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but owned by another user
    return True


class DeployRegistry:
    """Store-backed registry of deployed pipelines (read by deploy/manage/visualize)."""

    def __init__(self, store: Store, *, org_id: str = "local") -> None:
        self._store = store
        self._org = org_id

    def register(self, entry: DeployEntry) -> None:
        self._store.put_record(
            _REGISTRY_KIND, entry.name, entry.model_dump(mode="json"), org_id=self._org
        )

    def get(self, name: str) -> DeployEntry | None:
        rec = self._store.get_record(_REGISTRY_KIND, name, org_id=self._org)
        return None if rec is None else DeployEntry.model_validate(rec)

    def entries(self) -> list[DeployEntry]:
        rows = self._store.list_records(_REGISTRY_KIND, org_id=self._org)
        return sorted((DeployEntry.model_validate(r) for r in rows), key=lambda e: e.name)

    def set_status(self, name: str, status: DeployStatus) -> None:
        entry = self.get(name)
        if entry is not None:
            entry.status = status
            self.register(entry)

    def remove(self, name: str) -> None:
        self._store.delete_record(_REGISTRY_KIND, name, org_id=self._org)

    def reconcile_liveness(self) -> list[str]:
        """Mark registry rows whose PID is gone as ``DEAD``; return their names."""
        dead: list[str] = []
        for entry in self.entries():
            if entry.status == DeployStatus.RUNNING and not _pid_alive(entry.pid):
                self.set_status(entry.name, DeployStatus.DEAD)
                dead.append(entry.name)
        return dead


def default_run_fn(project_dir: str | Path) -> RunFn:
    """The default cycle: run the project's pipeline bootstrap once.

    A project can ship a richer pipeline; this keeps deploy honest with the engine
    bootstrap (an empty pipeline is a valid no-op) so the always-on machinery is
    exercised end to end without a live model call.
    """
    import asyncio

    from crawfish.engine import Engine

    def _run(ctx: RunContext) -> None:
        asyncio.run(Engine(ctx.store).run_pipeline([], ctx=ctx))

    return _run


def _import_project(project_dir: str | Path) -> object | None:
    """Import a project's ``pipeline.py`` and return the module (or ``None``).

    The project dir goes on ``sys.path`` so the module's sibling imports resolve. Any
    import error yields ``None`` rather than propagating — a broken project must never
    take down the supervisor or the manage view.
    """
    import importlib.util
    import sys as _sys

    root = Path(project_dir).resolve()
    pipeline_py = root / "pipeline.py"
    if not pipeline_py.exists():
        return None
    if str(root) not in _sys.path:
        _sys.path.insert(0, str(root))
    try:
        spec = importlib.util.spec_from_file_location(
            f"_crawfish_pipeline_{root.name}", pipeline_py
        )
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    except Exception:  # noqa: BLE001
        return None
    return module


def load_workflow(project_dir: str | Path) -> object | None:
    """Discover a project's deployable pipeline by convention.

    A deployable project ships a ``pipeline.py`` at its root exposing
    ``build_pipeline() -> Workflow``. We import that file and call the factory. Any
    failure — no file, no factory, an import/build error, or a non-Workflow return —
    yields ``None`` so the caller can fall back to the bootstrap rather than crash the
    daemon. Returns a fresh ``Workflow`` each call (one per cycle), so in-memory node
    state never accretes across runs.
    """
    from crawfish.workflow import Workflow

    module = _import_project(project_dir)
    if module is None:
        return None
    builder = getattr(module, "build_pipeline", None)
    if not callable(builder):
        return None
    try:
        workflow = builder()
    except Exception:  # noqa: BLE001 — a broken project must not take down the supervisor
        return None
    return workflow if isinstance(workflow, Workflow) else None


def load_trigger(project_dir: str | Path) -> str | None:
    """Discover a project's declared firing cadence as a cron string, or ``None``.

    A project declares *how it fires* as a first-class trigger object — a module-level
    ``TRIGGER`` (a :class:`~crawfish.triggers.CronTrigger`, exposing ``.schedule``) — or
    a plain ``SCHEDULE`` cron string. ``craw deploy`` uses this when ``--schedule`` is
    omitted, so cadence lives in the project, not the command line. Returns ``None`` for
    a project with no cron trigger (e.g. webhook-driven, or none declared).
    """
    module = _import_project(project_dir)
    if module is None:
        return None
    trigger = getattr(module, "TRIGGER", None)
    schedule = getattr(trigger, "schedule", None)
    if isinstance(schedule, str):
        return schedule
    schedule = getattr(module, "SCHEDULE", None)
    return schedule if isinstance(schedule, str) else None


def _discover_run_fn(project_dir: str | Path) -> RunFn:
    """The deployed cycle: run the project's discovered Workflow, else the bootstrap."""
    import asyncio

    root = Path(project_dir).resolve()
    if load_workflow(root) is None:
        return default_run_fn(root)

    def _run(ctx: RunContext) -> None:
        workflow = load_workflow(root)  # fresh per cycle
        if workflow is None:
            return
        asyncio.run(workflow.run(ctx=ctx))  # type: ignore[attr-defined]

    return _run


class Supervisor:
    """The always-on loop: schedule → fire → record, with ledger-backed resume.

    Construct with the pipeline ``name``, a :class:`~crawfish.store.base.Store`, the
    cycle ``run_fn``, and an optional cron ``schedule``. :meth:`serve` blocks; tests
    drive :meth:`run_cycle` / :meth:`due` directly with an injected clock.
    """

    def __init__(
        self,
        name: str,
        store: Store,
        run_fn: RunFn,
        *,
        schedule: str | None = None,
        org_id: str = "local",
        version: str = "0.1.0",
        backend: str = "command",
        secrets: Sequence[str] = (),
    ) -> None:
        self.name = name
        self.store = store
        self.run_fn = run_fn
        self.schedule = parse_schedule(schedule) if schedule else None
        self.org_id = org_id
        self.version = version
        self.backend = backend
        self.secrets = list(secrets)  # known secret values, for intrinsic scrubbing
        self.surface = ObserverSurface(store, org_id=org_id)
        self.ledger = ExecutionLedger(store, org_id=org_id)

    def reconcile(self) -> dict[str, list[str]]:
        """On (re)start, resume/retry orphaned runs via the ledger (CRA-134)."""
        result = self.ledger.reconcile()
        if result["retried"]:
            self.surface.emit(
                ObserverEvent(
                    pipeline=self.name,
                    kind="deploy.resumed",
                    severity=Severity.INFO,
                    detail=f"reconciled {len(result['retried'])} orphaned run(s) for retry",
                    observer="supervisor",
                )
            )
        return result

    def due(self, now: datetime) -> bool:
        """Whether a cycle should fire at ``now`` (always, if no schedule)."""
        return True if self.schedule is None else self.schedule.matches(now)

    def run_cycle(self, now: datetime | None = None) -> str:
        """Execute one pipeline cycle, recording RunInfo + ledger state.

        A raised exception inside ``run_fn`` is caught and recorded as a failed run +
        a critical observer event — the supervisor stays alive (auto-restart).
        """
        ts = (now or datetime.now(UTC)).timestamp()
        run_id = new_id()
        ctx = RunContext(store=self.store, run_id=run_id, org_id=self.org_id)
        self.surface.put_run_info(
            RunInfo(
                pipeline=self.name,
                run_id=run_id,
                status="running",
                backend=self.backend,
                version=self.version,
                started_at=ts,
            )
        )
        self.ledger.record_run(
            run_id, backend=self.backend, status=ExecState.RUNNING, version=self.version
        )
        status, state = "done", ExecState.DONE
        try:
            self.run_fn(ctx)
        except Exception as exc:  # noqa: BLE001 — supervisor must survive any cycle
            status, state = "failed", ExecState.FAILED
            # Scrub the exception text intrinsically: an exception can carry a raw
            # credential (it may quote fluid input). We redact both the known secret
            # *values* and the credential-shaped patterns, so the failure event is safe
            # even when the store is not itself a ScrubbingStore (defense in depth).
            self.surface.emit(
                ObserverEvent(
                    pipeline=self.name,
                    kind="run.failed",
                    severity=Severity.CRITICAL,
                    detail=redact(str(exc), self.secrets)[:200],
                    observer="supervisor",
                    run_id=run_id,
                )
            )
        self.ledger.record_run(run_id, backend=self.backend, status=state, version=self.version)
        self.surface.put_run_info(
            RunInfo(
                pipeline=self.name,
                run_id=run_id,
                status=status,
                backend=self.backend,
                version=self.version,
                cost_usd=ctx.cost_budget.spent_usd,
                started_at=ts,
                finished_at=(now or datetime.now(UTC)).timestamp(),
            )
        )
        return run_id

    def process_items(self, items: Sequence[str], handler: Callable[[str], None]) -> list[str]:
        """Process fan-out ``items`` exactly once across restarts (ledger resume).

        Items already marked ``DONE`` in the ledger are **skipped** — so after a crash
        and restart, only unfinished items re-run. Each item is marked ``DONE`` only
        after its handler returns; a handler that raises leaves the item unmarked so it
        retries next time. Returns the item ids processed in this call.
        """
        done = self.ledger.completed_items(self.name)
        processed: list[str] = []
        for item in items:
            if item in done:
                continue
            handler(item)
            self.ledger.mark_item(self.name, item, ExecState.DONE)
            processed.append(item)
        return processed

    def serve(
        self,
        *,
        max_cycles: int | None = None,
        now_fn: Callable[[], datetime] | None = None,
        sleep_fn: Callable[[float], None] | None = None,
        stop_flag: Callable[[], bool] | None = None,
    ) -> int:
        """Block in the always-on loop. Returns the number of cycles fired.

        Pure-logic seams (``now_fn``/``sleep_fn``/``stop_flag``/``max_cycles``) make
        the loop testable without real time. With no schedule, fires every tick; with
        a cron schedule, sleeps to the next matching minute.
        """
        import time as _time

        now_fn = now_fn or (lambda: datetime.now(UTC))
        sleep_fn = sleep_fn or _time.sleep
        stop_flag = stop_flag or (lambda: False)
        self.reconcile()
        fired = 0
        while not stop_flag():
            if max_cycles is not None and fired >= max_cycles:
                break
            now = now_fn()
            if self.due(now):
                self.run_cycle(now)
                fired += 1
            if self.schedule is None:
                sleep_fn(1.0)
            else:
                delay = max(1.0, (self.schedule.next_after(now) - now).total_seconds())
                sleep_fn(delay)
        return fired


# Spawn seam: build the detached child command. No secret ever appears here.
Spawner = Callable[[list[str], Path, Path], int]


def _default_spawn(argv: list[str], cwd: Path, log: Path) -> int:
    """Spawn a detached, session-leader child; return its PID. No shell, no secrets."""
    log.parent.mkdir(parents=True, exist_ok=True)
    handle = log.open("ab")
    proc = subprocess.Popen(  # noqa: S603 — argv is built from static parts only
        argv,
        cwd=str(cwd),
        stdout=handle,
        stderr=handle,
        stdin=subprocess.DEVNULL,
        start_new_session=True,  # survive the shell closing (setsid)
        close_fds=True,
    )
    return proc.pid


def deploy(
    project_dir: str | Path,
    *,
    name: str,
    store: Store,
    schedule: str | None = None,
    backend: str = "daemon",
    spawn: Spawner | None = None,
    org_id: str = "local",
) -> DeployEntry:
    """Detach the project's pipeline as an always-on supervisor and register it.

    Validates the schedule up front, spawns the detached ``craw _supervise`` child
    (argv carries only the pipeline name + dir — never a secret), and writes the
    deploy-registry entry ``craw manage`` reads.

    When ``schedule`` is omitted, the project's own declared trigger (a module-level
    ``TRIGGER``/``SCHEDULE`` in its ``pipeline.py``) is used — so cadence lives in the
    project, not the command line.
    """
    if schedule is None:
        schedule = load_trigger(project_dir)
    if schedule is not None:
        parse_schedule(schedule)  # fail fast on a bad cron / interval expression
    root = Path(project_dir).resolve()
    spawn = spawn or _default_spawn
    registry = DeployRegistry(store, org_id=org_id)

    # Replacing a still-live deployment of the same name would orphan its process —
    # surface that instead of silently leaking a PID.
    prior = registry.get(name)
    if prior is not None and prior.status == DeployStatus.RUNNING and _pid_alive(prior.pid):
        ObserverSurface(store, org_id=org_id).emit(
            ObserverEvent(
                pipeline=name,
                kind="deploy.replaced",
                severity=Severity.WARN,
                detail=f"redeployed over live pid {prior.pid}; stop it via `craw manage stop`",
                observer="deploy",
            )
        )
    log_path = root / ".crawfish" / "deploys" / f"{name}.log"
    argv = [
        sys.executable,
        "-m",
        "crawfish.cli",
        "_supervise",
        name,
        "--dir",
        str(root),
    ]
    if schedule is not None:
        argv += ["--schedule", schedule]
    pid = spawn(argv, root, log_path)
    entry = DeployEntry(
        name=name,
        pid=pid,
        dir=str(root),
        session=f"crawfish/{name}",
        backend=backend,
        schedule=schedule,
        log_path=str(log_path),
    )
    registry.register(entry)
    return entry


def stop(
    name: str, *, store: Store, org_id: str = "local", kill: Callable[[int], None] | None = None
) -> bool:
    """Stop a deployed pipeline: signal its process and clear its registry status.

    Returns True if an entry was found. ``kill`` is injectable for tests.
    """
    registry = DeployRegistry(store, org_id=org_id)
    entry = registry.get(name)
    if entry is None:
        return False
    sender = kill or (lambda pid: os.kill(pid, signal.SIGTERM))
    if _pid_alive(entry.pid):
        try:
            sender(entry.pid)
        except ProcessLookupError:
            pass
    registry.set_status(name, DeployStatus.STOPPED)
    return True


def supervise_main(name: str, project_dir: str | Path, schedule: str | None = None) -> int:
    """Entry point for the detached child process (``craw _supervise``).

    Opens the project's Store wrapped in a ScrubbingStore (so nothing the cycle
    writes can leak a secret), then blocks in the supervisor loop.
    """
    from crawfish.secrets import ScrubbingStore, SecretManager
    from crawfish.store import SqliteStore

    root = Path(project_dir)
    db = root / ".crawfish" / "crawfish.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    secrets = SecretManager(env=None)
    store = ScrubbingStore(SqliteStore(db), secrets=secrets.values)
    sup = Supervisor(name, store, _discover_run_fn(root), schedule=schedule, secrets=secrets.values)
    sup.serve()
    return 0
