"""``craw code deploy <pipeline>`` (+ default observers) and ``craw code fleet``.

The operate plane's *deploy / fleet* verbs (UNFILED-DEPLOY, M4.5). They are the
agent-friendly veneer over the **shipped** deploy + manage paths — they add no second
supervisor and no second management surface:

* **deploy** composes :func:`crawfish.deploy.deploy` (the detached supervisor whose argv
  carries only name/dir/schedule — **never a secret**, operate-layer rule 2) and
  *additionally* scaffolds default Observer rules (cost-spike / failure-rate / stuck) into
  ``observers/`` when that directory is empty, so a freshly deployed pipeline is watched by
  default. The scaffolded defaults are pure rules — no ``judge=`` Definition, so no
  LLM-judge surface and no fluid input.
* **fleet** composes :func:`crawfish.manage.manage_list` / ``format_table`` / ``stop`` /
  ``restart_target`` / ``logs`` 1:1 — ``fleet`` (list), ``fleet stop|restart|tail <name>``.

Security spine: the deploy **target** (pipeline name) and the supervisor session name are
STATIC author/CLI config — a fluid value can never choose a deploy target (core rule 2 /
ALG-3). Sink targets in the deployed pipeline remain static-only. Every registry read is
``org_id``-scoped (CRA-275): a deploy in org A is invisible to ``fleet --org b``. Protocols
only: the Store is opened through :func:`crawfish.manage.store_for_dir`.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import TYPE_CHECKING

from crawfish.code import (
    EXIT_OK,
    SCHEMA_VERSIONS,
    ErrorCode,
    emit_error,
    emit_json,
)

if TYPE_CHECKING:
    from crawfish.deploy import Spawner
    from crawfish.store.base import Store

SCHEMA_VERSIONS.setdefault("code.deploy", (1, 0))  # type: ignore[attr-defined]
SCHEMA_VERSIONS.setdefault("code.fleet", (1, 0))  # type: ignore[attr-defined]

VERB_NAME = "deploy"  # module name; registers TWO verbs (deploy + fleet)

#: ``fleet`` sub-action requested an unknown pipeline (exit 1).
EXIT_NO_PIPELINE = 1
#: ``deploy`` was given an invalid cron/interval schedule (exit 2).
EXIT_BAD_SCHEDULE = 2

#: The default Observer rules a fresh deploy is watched by (pure rules; no LLM-judge surface).
_DEFAULT_OBSERVERS = ("cost_spike", "failure_rate", "stuck")


def register(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    """Register both ``craw code deploy`` and ``craw code fleet`` (self-registering)."""
    from crawfish.code.cli import add_common_args

    deploy_p = subparsers.add_parser(
        "deploy", help="deploy a pipeline + default observers (UNFILED-DEPLOY)"
    )
    deploy_p.add_argument("pipeline", help="the pipeline name (a STATIC deploy target)")
    deploy_p.add_argument("--dir", default=".", help="project directory holding the pipeline")
    deploy_p.add_argument(
        "--schedule", default=None, help="cron firing cadence (else the project's TRIGGER)"
    )
    deploy_p.add_argument(
        "--observers",
        choices=("default", "none"),
        default="default",
        help="scaffold default cost/failure/stuck observers when observers/ is empty",
    )
    add_common_args(deploy_p)
    deploy_p.set_defaults(func=_cmd_deploy)

    fleet_p = subparsers.add_parser(
        "fleet", help="list/stop/restart/tail deployed pipelines (UNFILED-DEPLOY)"
    )
    fleet_p.add_argument(
        "action",
        nargs="?",
        choices=("list", "stop", "restart", "tail"),
        default="list",
        help="list (default) | stop | restart | tail",
    )
    fleet_p.add_argument(
        "target", nargs="?", default=None, help="the pipeline name (stop/restart/tail)"
    )
    add_common_args(fleet_p)
    fleet_p.set_defaults(func=_cmd_fleet)


# --------------------------------------------------------------------------- deploy


def scaffold_default_observers(project_dir: str | Path) -> list[str]:
    """Scaffold the default cost/failure/stuck Observer rules iff ``observers/`` is empty.

    Returns the names scaffolded (``[]`` when ``observers/`` already has content — never
    clobbers an authored watcher). Each is a pure, read-only rule watcher (``role:
    observer``): it never fires a consequential action and carries no ``judge=`` Definition,
    so it adds no LLM-judge / fluid surface.
    """
    root = Path(project_dir)
    observers = root / "observers"
    # Only scaffold into an EMPTY observers/ — an authored watcher is never overwritten.
    if observers.exists() and any(observers.iterdir()):
        return []
    observers.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    for name in _DEFAULT_OBSERVERS:
        folder = observers / name
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "instructions.md").write_text(_observer_rule_template(name))
        written.append(name)
    return written


def _observer_rule_template(name: str) -> str:
    """An authored default-observer rule (pure watcher; no consequential action)."""
    what = {
        "cost_spike": "a sudden jump in per-run cost vs the recent band",
        "failure_rate": "a rise in the failed-run fraction over the window",
        "stuck": "a run that stops making fan-out progress (no new DONE items)",
    }[name]
    return (
        f"---\nrole: observer\nkind: {name}\n---\n"
        f"You are the default {name!r} watcher. Flag {what}. You are read-only: you never\n"
        "fire a consequential action and you judge nothing with a model — you raise an\n"
        "ObserverEvent the dashboard renders. (Scaffolded by `craw code deploy`.)\n"
    )


def deploy_pipeline(
    pipeline: str,
    *,
    project_dir: str | Path,
    store: Store,
    schedule: str | None = None,
    observers: str = "default",
    org_id: str = "local",
    spawn: Spawner | None = None,
) -> dict[str, object]:
    """Deploy ``pipeline`` via the shipped supervisor + scaffold default observers.

    Composes :func:`crawfish.deploy.deploy` (no second supervisor); the spawned argv carries
    only name/dir/schedule, never a secret (operate-layer rule 2). ``observers="default"``
    scaffolds cost/failure/stuck rules when ``observers/`` is empty. Returns the
    ``craw.code.deploy.v1`` body. Raises :class:`ValueError` on a bad schedule.
    """
    from crawfish.deploy import deploy as _deploy

    scaffolded: list[str] = []
    if observers == "default":
        scaffolded = scaffold_default_observers(project_dir)

    # ``deploy`` validates the schedule (raises ValueError on a bad cron) and builds the
    # detached child whose argv is static parts only — no secret ever crosses it.
    entry = _deploy(
        project_dir,
        name=pipeline,
        store=store,
        schedule=schedule,
        org_id=org_id,
        spawn=spawn,
    )
    return {
        "pipeline": entry.name,
        "session": entry.session,  # "crawfish/<name>" — never carries a secret
        "schedule": entry.schedule,
        "status": entry.status.value,
        "observers_scaffolded": scaffolded,
    }


def _cmd_deploy(args: argparse.Namespace) -> int:
    """``craw code deploy <pipeline> [--dir D] [--schedule S] [--observers default|none]``."""
    org = getattr(args, "org", "local")
    as_json = getattr(args, "as_json", False)
    project_dir = getattr(args, "dir", ".")
    Path(project_dir, ".crawfish").mkdir(parents=True, exist_ok=True)
    store = _store_for(project_dir)
    try:
        body = deploy_pipeline(
            args.pipeline,
            project_dir=project_dir,
            store=store,
            schedule=args.schedule,
            observers=args.observers,
            org_id=org,
        )
    except ValueError as exc:
        # A bad cron/interval is a usage error (exit 2). The remediation is static — the
        # rejected schedule string is not echoed back into the agent's instruction stream.
        return emit_error(
            ErrorCode.USAGE,
            remediation="Invalid --schedule; pass a valid cron/interval expression.",
            detail={"pipeline": args.pipeline, "error": exc.__class__.__name__},
            as_json=as_json,
        )
    finally:
        store.close()

    if as_json:
        emit_json("code.deploy", body, org=org)
    else:
        scaffolded = ", ".join(body["observers_scaffolded"]) or "none"  # type: ignore[arg-type]
        print(
            f"deployed {body['pipeline']} ({body['session']}) "
            f"schedule={body['schedule'] or '—'} observers={scaffolded}"
        )
    return EXIT_OK


# --------------------------------------------------------------------------- fleet


def fleet_rows(store: Store, *, org_id: str = "local") -> list[dict[str, object]]:
    """The ``craw.code.fleet.v1`` pipeline rows, composed 1:1 from ``manage_list``.

    Each row mirrors a :class:`~crawfish.manage.PipelineStatus` (org-scoped — another org's
    deploys never appear). ``name``/``status``/``next_fire`` are stable identifiers; cost is
    the scrubbed ``RunInfo`` rollup ``craw manage`` shows.
    """
    from crawfish.manage import manage_list

    return [
        {
            "name": row.name,
            "status": row.status,
            "uptime_s": row.uptime_s,
            "next_fire": row.next_fire,
            "cost_today_usd": row.cost_today_usd,
        }
        for row in manage_list(store, org_id=org_id)
    ]


def _cmd_fleet(args: argparse.Namespace) -> int:
    """``craw code fleet [list|stop|restart|tail] [<name>] [--org ID] [--json]``."""
    org = getattr(args, "org", "local")
    as_json = getattr(args, "as_json", False)
    action = getattr(args, "action", "list") or "list"
    store = _store_for(".")
    try:
        if action == "list":
            rows = fleet_rows(store, org_id=org)
            if as_json:
                emit_json("code.fleet", {"pipelines": rows}, org=org)
            else:
                from crawfish.manage import format_table, manage_list

                print(format_table(manage_list(store, org_id=org)))
            return EXIT_OK

        # stop / restart / tail all require a target name.
        target = getattr(args, "target", None)
        if not target:
            return emit_error(
                ErrorCode.USAGE,
                remediation=f"`craw code fleet {action}` needs a pipeline name.",
                detail={"action": action},
                as_json=as_json,
            )
        return _fleet_action(action, target, store=store, org=org, as_json=as_json)
    finally:
        store.close()


def _fleet_action(action: str, target: str, *, store: Store, org: str, as_json: bool) -> int:
    """Map ``stop``/``restart``/``tail`` 1:1 onto the shipped manage paths."""
    from crawfish.manage import manage_list, restart_target

    if action == "stop":
        from crawfish.deploy import stop

        ok = stop(target, store=store, org_id=org)
        return _fleet_result("stop", target, ok, org=org, as_json=as_json)
    if action == "restart":
        ok = restart_target(target, store=store, org_id=org)
        return _fleet_result("restart", target, ok, org=org, as_json=as_json)

    # tail: stream the target's recorded run events (read-only) — the manage ``logs`` path.
    from crawfish.inspector import tail_events

    for row in manage_list(store, org_id=org):
        if row.name == target:
            lines: list[str] = []
            for ri in row.runs:
                for event in tail_events(store, ri.run_id, after_seq=-1):
                    import json as _json

                    lines.append(_json.dumps(event, sort_keys=True))
            if as_json:
                emit_json(
                    "code.fleet",
                    {"action": "tail", "name": target, "events": lines},
                    org=org,
                )
            else:
                print("\n".join(lines) if lines else f"(no events for {target})")
            return EXIT_OK
    return emit_error(
        ErrorCode.NOT_FOUND,
        remediation=f"No deployed pipeline {target!r} in org {org!r}.",
        detail={"name": target},
        as_json=as_json,
    )


def _fleet_result(action: str, target: str, ok: bool, *, org: str, as_json: bool) -> int:
    """Emit a stop/restart result envelope + the closed exit code."""
    if not ok:
        return emit_error(
            ErrorCode.NOT_FOUND,
            remediation=f"No deployed pipeline {target!r} in org {org!r}.",
            detail={"name": target},
            as_json=as_json,
        )
    body: dict[str, object] = {"action": action, "name": target, "result": "ok"}
    if as_json:
        emit_json("code.fleet", body, org=org)
    else:
        print(f"{action} {target}: ok")
    return EXIT_OK


# --------------------------------------------------------------------------- shared


def _store_for(project_dir: str) -> Store:
    """Open the per-project Store through the protocol-returning factory (never a backend)."""
    from crawfish.manage import store_for_dir

    return store_for_dir(project_dir)
