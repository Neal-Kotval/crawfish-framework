"""The ``craw`` CLI.

Command surface: ``init`` (scaffold a working project — the 5-minute wow),
``list`` (module discovery), ``install`` (capability consent), ``freeze``
(lockfile + integrity), ``publish`` (registry stub), ``run`` / ``dev`` (+ ``--estimate``
cost preview), ``test``, ``build`` (Containerfile),
``inspect`` / ``logs`` (run inspector).
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from importlib.metadata import version as _pkg_version
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from crawfish.store.base import Store


def _version() -> str:
    try:
        return _pkg_version("crawfish")
    except Exception:  # pragma: no cover - source checkout without install
        return "0.0.0+dev"


# --------------------------------------------------------------------------- run
def _cmd_run(_args: argparse.Namespace) -> int:
    from crawfish.engine import run_pipeline

    outputs = asyncio.run(run_pipeline([]))
    print(f"pipeline ok: {len(outputs)} output(s)")
    return 0


# --------------------------------------------------------------------------- dev
def _cmd_dev(args: argparse.Namespace) -> int:
    from crawfish.core.context import RunContext
    from crawfish.definition import Definition
    from crawfish.runtime import MockRuntime, run_team
    from crawfish.store import SqliteStore

    definition = Definition.from_package(args.path)
    inputs: dict[str, object] = {}
    for pair in args.input or []:
        key, _, value = pair.partition("=")
        inputs[key] = value

    if args.estimate:
        from crawfish.config import load_models_config
        from crawfish.cost import estimate_cost

        # Resolve models the same way the runtime will (aliases + configured default),
        # so the preview never drifts from the actual run.
        est = estimate_cost(definition, items=args.items, config=load_models_config())
        print(
            f"estimated cost: ${est.total_usd:.4f} for {args.items} item(s) "
            f"(team of {est.team_size})"
        )
        return 0

    async def _go() -> str:
        ctx = RunContext(store=SqliteStore())
        result = await run_team(definition, inputs, ctx, MockRuntime())
        return result.text

    print(asyncio.run(_go()))
    return 0


# -------------------------------------------------------------------------- list
def _cmd_list(args: argparse.Namespace) -> int:
    from crawfish.discovery import Registry

    reg = Registry.discover(args.dir)
    if not reg.units:
        print("no units discovered")
        return 0
    for (kind, name), ref in sorted(reg.units.items()):
        print(f"{kind:11} {name:24} {ref.origin}")
    return 0


# ------------------------------------------------------------------------ install
def _package_name(path: str) -> str:
    """The grant key for a unit/package: its manifest name, else the directory stem."""
    from crawfish.config import load_manifest

    try:
        name = load_manifest(path).name
    except Exception:
        name = ""
    return name or Path(path).resolve().name


def _cmd_install(args: argparse.Namespace) -> int:
    from crawfish.secrets import (
        AutoConsent,
        ConsentDeclined,
        ConsentRequest,
        DenyConsent,
        consent_install,
        read_capabilities,
    )

    caps = read_capabilities(args.path)
    package = _package_name(args.path)
    request = ConsentRequest.from_capabilities(package, caps)
    # Surface the STATIC declared capabilities — secrets by REFERENCE, never value.
    print(f"'{package}' requests — {request.summary()}")

    # Consent decider: --yes is an explicit non-interactive approval; otherwise a
    # non-interactive context is fail-closed (DenyConsent), so nothing self-approves.
    decider = AutoConsent() if args.yes else DenyConsent()
    store = _open_store(args.path)
    try:
        grant = consent_install(package, caps, store=store, decider=decider)
    except ConsentDeclined:
        print("capabilities NOT consented; no grant recorded (re-run with --yes to consent).")
        return 1
    finally:
        store.close()
    print(f"capabilities consented; grant {grant.grant_id} recorded for '{package}'.")
    return 0


# ------------------------------------------------------------------------- freeze
def _cmd_freeze(args: argparse.Namespace) -> int:
    from crawfish.discovery import Registry

    reg = Registry.discover(args.dir)
    pins: dict[str, dict[str, str]] = {}
    for (kind, name), ref in sorted(reg.units.items()):
        integrity = ""
        target = Path(ref.target)
        if target.exists():
            data = (
                b"".join(sorted(p.read_bytes() for p in target.rglob("*") if p.is_file()))
                if target.is_dir()
                else target.read_bytes()
            )
            integrity = "sha256:" + hashlib.sha256(data).hexdigest()  # full digest
        pins[f"{kind}:{name}"] = {"origin": ref.origin, "integrity": integrity}
    lock = Path(args.dir) / "crawfish.lock"
    lock.write_text(json.dumps({"units": pins}, indent=2, sort_keys=True) + "\n")
    print(f"wrote {lock} ({len(pins)} unit(s))")
    return 0


def _cmd_publish(_args: argparse.Namespace) -> int:
    print("publish: the registry is Phase 2; nothing to publish yet.")
    return 0


# --------------------------------------------------------------------------- test
def _cmd_test(args: argparse.Namespace) -> int:
    from crawfish.core.context import RunContext
    from crawfish.definition import Definition
    from crawfish.runtime import MockRuntime
    from crawfish.store import SqliteStore
    from crawfish.testing import run_fixtures

    definition = Definition.from_package(args.path)
    results = asyncio.run(
        run_fixtures(
            args.fixtures,
            definition,
            MockRuntime(),
            ctx_factory=lambda: RunContext(store=SqliteStore()),
        )
    )
    passed = sum(1 for r in results if r.passed)
    for r in results:
        print(f"{'PASS' if r.passed else 'FAIL'}  {r.name}")
    print(f"{passed}/{len(results)} fixtures passed")
    return 0 if passed == len(results) else 1


# -------------------------------------------------------------------------- build
def _cmd_build(args: argparse.Namespace) -> int:
    from crawfish.build import plan_build, write_containerfile
    from crawfish.config import load_manifest

    manifest = load_manifest(args.dir)
    plan = plan_build(manifest, lock_present=(Path(args.dir) / "crawfish.lock").exists())
    dest = write_containerfile(manifest, Path(args.dir), lock_present=plan.lock_present)
    print(f"wrote {dest} → image {plan.image} (base {plan.base_image})")
    return 0


# ------------------------------------------------------------------ inspect / logs
def _open_store(project_dir: str) -> Store:
    from crawfish.store import SqliteStore

    db = Path(project_dir) / ".crawfish" / "crawfish.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    return SqliteStore(db)


def _cmd_inspect(args: argparse.Namespace) -> int:
    from crawfish.inspector import format_report, inspect_run

    report = inspect_run(_open_store(args.dir), args.run_id)
    print(format_report(report))
    return 0 if report.found else 1


def _cmd_logs(args: argparse.Namespace) -> int:
    from crawfish.inspector import tail_events

    for event in tail_events(_open_store(args.dir), args.run_id, after_seq=args.after):
        print(json.dumps(event))
    return 0


# ------------------------------------------------------------------------- doctor
def _cmd_doctor(args: argparse.Namespace) -> int:
    from crawfish.doctor import diagnose

    report = diagnose(args.dir)
    print(report.text())
    return 0 if report.ok else 1


# ------------------------------------------------------------------------- deploy
def _project_name(project_dir: str) -> str:
    from crawfish.config import load_manifest

    return load_manifest(project_dir).name


def _cmd_deploy(args: argparse.Namespace) -> int:
    from crawfish.deploy import deploy
    from crawfish.manage import register_deployment

    name = args.name or _project_name(args.dir)
    entry = deploy(args.dir, name=name, store=_open_store(args.dir), schedule=args.schedule)
    # Record it in the global index so `craw manage` (no --dir) finds it from anywhere.
    register_deployment(name, str(Path(args.dir).resolve()))
    # Reflect the *resolved* schedule (entry.schedule), which may come from the project's
    # declared TRIGGER even when --schedule was omitted.
    when = f"on schedule {entry.schedule!r}" if entry.schedule else "continuously"
    print(f"deployed {name} (pid {entry.pid}, session {entry.session}) — runs {when}")
    print(f"  logs: {entry.log_path}")
    print("  manage: craw manage    dashboard: craw visualize")
    return 0


# ------------------------------------------------------------------------- manage
def _cmd_manage(args: argparse.Namespace) -> int:
    from crawfish.deploy import stop
    from crawfish.inspector import tail_events
    from crawfish.manage import (
        PipelineStatus,
        format_table,
        global_manage_list,
        interactive_manage,
        manage_list,
        resolve_deployment_dir,
        restart_target,
        store_for_dir,
    )

    # --dir scopes to one project; omitting it manages ALL deployments via the global index.
    scoped = args.dir is not None

    def _store_for(name: str | None) -> Store | None:
        if scoped:
            return _open_store(args.dir)
        target_dir = resolve_deployment_dir(name) if name else None
        return store_for_dir(target_dir) if target_dir else None

    if args.action in ("stop", "restart", "logs"):
        store = _store_for(args.target)
        if store is None:
            print(f"no such pipeline: {args.target}")
            return 1
        if args.action == "stop":
            ok = stop(args.target, store=store)
            print(f"stopped {args.target}" if ok else f"no such pipeline: {args.target}")
            return 0 if ok else 1
        if args.action == "restart":
            ok = restart_target(args.target, store=store)
            print(f"restarted {args.target}" if ok else f"no such pipeline: {args.target}")
            return 0 if ok else 1
        for row in manage_list(store):  # logs
            if row.name == args.target:
                for ri in row.runs:
                    for event in tail_events(store, ri.run_id, after_seq=-1):
                        print(json.dumps(event))
                return 0
        print(f"no such pipeline: {args.target}")
        return 1

    # Default (list): global view unless --dir scopes it.
    def _rows() -> list[PipelineStatus]:
        return manage_list(_open_store(args.dir)) if scoped else global_manage_list()

    # Interactive TUI on a real terminal; static table when piped/redirected or --plain.
    if not getattr(args, "plain", False) and sys.stdout.isatty():
        return interactive_manage(_rows)
    print(format_table(_rows(), show_dir=not scoped))
    return 0


# ---------------------------------------------------------------------- visualize
def _cmd_visualize(args: argparse.Namespace) -> int:
    from crawfish.visualize import LOOPBACK, serve_dashboard

    server = serve_dashboard(_open_store(args.dir), port=args.port)
    host, port = str(server.server_address[0]), server.server_address[1]
    print(f"crawfish dashboard → http://{host}:{port}  (loopback only; Ctrl-C to stop)")
    assert host == LOOPBACK  # never bind a public interface
    try:
        server.serve_forever()
    except KeyboardInterrupt:  # pragma: no cover - interactive
        print("\nstopped")
    finally:
        server.server_close()
    return 0


# ----------------------------------------------------------------- dashboard (CRA-181)
def _cmd_dashboard(args: argparse.Namespace) -> int:
    """Serve the auto-dashboard projected from the typed emission stream (loopback)."""
    from crawfish.visualize import LOOPBACK, serve_emission_dashboard

    server = serve_emission_dashboard(_open_store(args.dir), port=args.port, since=args.since)
    host, port = str(server.server_address[0]), server.server_address[1]
    print(f"crawfish emission dashboard → http://{host}:{port}  (loopback only; Ctrl-C to stop)")
    assert host == LOOPBACK  # never bind a public interface
    try:
        server.serve_forever()
    except KeyboardInterrupt:  # pragma: no cover - interactive
        print("\nstopped")
    finally:
        server.server_close()
    return 0


# -------------------------------------------------------------------------- export
def _cmd_export(args: argparse.Namespace) -> int:
    from crawfish.ccexport import export_claude_code
    from crawfish.definition import Definition

    definition = Definition.from_package(args.path)
    paths = export_claude_code(definition, Path(args.dir), skill=args.skill)
    for p in paths:
        print(f"wrote {p}")
    return 0


# ----------------------------------------------------------------- _supervise (hidden)
def _cmd_supervise(args: argparse.Namespace) -> int:
    from crawfish.deploy import supervise_main

    return supervise_main(args.name, args.dir, schedule=args.schedule)


# --------------------------------------------------------------------------- demo
def _cmd_demo(args: argparse.Namespace) -> int:
    """Run the Milestone-F all-nine-features end-to-end scenario.

    ``craw demo`` runs it deterministically on the mock runtime (zero cost, CI-safe);
    ``craw demo --live`` runs it against the real ``claude -p`` backend and records
    fresh cassettes. See ``demo/triage-bot/self_improve.py`` and ``RUNBOOK.md``.
    """
    import importlib.util
    from pathlib import Path as _Path

    # The scenario lives beside the demo project (not inside the package), so load it
    # by path. ``--dir`` lets a caller point at a relocated copy of demo/triage-bot.
    candidates = []
    if args.dir is not None:
        candidates.append(_Path(args.dir).resolve())
    else:
        # Default: cwd's demo/triage-bot, else the repo's (relative to this package).
        candidates.append(_Path.cwd() / "demo" / "triage-bot")
        repo_demo = _Path(__file__).resolve().parents[4] / "demo" / "triage-bot"
        candidates.append(repo_demo)
    demo_dir = next((c for c in candidates if (c / "self_improve.py").exists()), candidates[0])
    module_path = demo_dir / "self_improve.py"
    if not module_path.exists():
        print(f"no demo scenario at {module_path}; pass --dir <demo/triage-bot>")
        return 1
    spec = importlib.util.spec_from_file_location("crawfish_demo_self_improve", module_path)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        print(f"could not load {module_path}")
        return 1
    module = importlib.util.module_from_spec(spec)
    # Register before exec so dataclass/typing forward-ref resolution can find the module.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    result = module.run_self_improvement(live=args.live, record=args.live)
    print(result.summary())
    return 0 if result.passed() else 1


# --------------------------------------------------------------------------- init
def _cmd_init(args: argparse.Namespace) -> int:
    from crawfish.scaffold import scaffold_project

    root = scaffold_project(args.name)
    print(f"created project at {root}")
    print("next:")
    print(f"  cd {root}")
    print('  craw dev definitions/triage-bot -i project=acme -i "ticket_body=login is broken"')
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="craw", description="Crawfish CLI")
    parser.add_argument("--version", action="version", version=f"crawfish {_version()}")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("run", help="run the project's pipeline")
    p.set_defaults(func=_cmd_run)

    p = sub.add_parser("dev", help="compile + run a Definition on the mock runtime")
    p.add_argument("path", help="path to a Definition directory")
    p.add_argument("-i", "--input", action="append", help="input as name=value (repeatable)")
    p.add_argument("--estimate", action="store_true", help="preview cost instead of running")
    p.add_argument("--items", type=int, default=1, help="item count for --estimate")
    p.set_defaults(func=_cmd_dev)

    p = sub.add_parser("demo", help="run the Milestone-F all-9-features end-to-end scenario")
    p.add_argument(
        "--live",
        action="store_true",
        help="run against the real `claude -p` backend and record fresh cassettes",
    )
    p.add_argument(
        "--dir",
        default=None,
        help="path to the demo/triage-bot directory (default: ./demo/triage-bot or the repo's)",
    )
    p.set_defaults(func=_cmd_demo)

    p = sub.add_parser("init", help="scaffold a new project with a working example")
    p.add_argument("name", nargs="?", default="crawfish-app", help="project directory name")
    p.set_defaults(func=_cmd_init)

    p = sub.add_parser("list", help="list discovered units")
    p.add_argument("--dir", default=".", help="project directory")
    p.set_defaults(func=_cmd_list)

    p = sub.add_parser("doctor", help="report project structure health")
    p.add_argument("--dir", default=".", help="project directory")
    p.set_defaults(func=_cmd_doctor)

    p = sub.add_parser("install", help="install a unit (surfaces capabilities for consent)")
    p.add_argument("path", help="path to the unit/package")
    p.add_argument("--yes", action="store_true", help="consent to the declared capabilities")
    p.set_defaults(func=_cmd_install)

    p = sub.add_parser("freeze", help="write crawfish.lock with integrity hashes")
    p.add_argument("--dir", default=".", help="project directory")
    p.set_defaults(func=_cmd_freeze)

    p = sub.add_parser("publish", help="publish to the registry (Phase 2 stub)")
    p.set_defaults(func=_cmd_publish)

    p = sub.add_parser("test", help="run fixtures against a Definition")
    p.add_argument("path", help="path to a Definition directory")
    p.add_argument("--fixtures", default="fixtures", help="fixtures directory")
    p.set_defaults(func=_cmd_test)

    p = sub.add_parser("build", help="generate a Containerfile from the manifest + lock")
    p.add_argument("--dir", default=".", help="project directory")
    p.set_defaults(func=_cmd_build)

    p = sub.add_parser("deploy", help="run the project's pipeline always-on (detached)")
    p.add_argument("name", nargs="?", default=None, help="deploy name (defaults to project name)")
    p.add_argument("--schedule", default=None, help='cron schedule, e.g. "0 8 * * *"')
    p.add_argument("--dir", default=".", help="project directory")
    p.set_defaults(func=_cmd_deploy)

    p = sub.add_parser("manage", help="see & control deployed pipelines")
    p.add_argument(
        "action",
        nargs="?",
        default="list",
        choices=["list", "stop", "restart", "logs"],
        help="management action",
    )
    p.add_argument("target", nargs="?", default=None, help="pipeline name (for stop/restart/logs)")
    p.add_argument(
        "--dir",
        default=None,
        help="project directory to scope to (omit to manage ALL deployments globally)",
    )
    p.add_argument(
        "--plain", action="store_true", help="print the static table instead of the interactive TUI"
    )
    p.set_defaults(func=_cmd_manage)

    p = sub.add_parser("visualize", help="serve the localhost dashboard (loopback only)")
    p.add_argument("--port", type=int, default=7878, help="dashboard port (127.0.0.1)")
    p.add_argument("--dir", default=".", help="project directory")
    p.set_defaults(func=_cmd_visualize)

    p = sub.add_parser(
        "dashboard",
        help="serve the auto-dashboard over the emission stream (loopback only)",
    )
    p.add_argument(
        "--port", type=int, default=7879, help="dashboard port (127.0.0.1; distinct from visualize)"
    )
    p.add_argument("--since", default=None, help="time window, e.g. -15m / -24h (default: all)")
    p.add_argument("--dir", default=".", help="project directory")
    p.set_defaults(func=_cmd_dashboard)

    p = sub.add_parser("export", help="export a Definition to another runtime")
    p.add_argument(
        "--claude-code",
        dest="path",
        metavar="PATH",
        required=True,
        help="path to a Definition directory to export as a Claude Code subagent",
    )
    p.add_argument(
        "--skill",
        action="store_true",
        help="also emit a .claude/skills/<name>/SKILL.md slash-command wrapper",
    )
    p.add_argument("--dir", default=".", help="project directory to write .claude/ into")
    p.set_defaults(func=_cmd_export)

    # hidden: the detached supervisor entry point that `craw deploy` spawns
    p = sub.add_parser("_supervise")
    p.add_argument("name")
    p.add_argument("--dir", default=".")
    p.add_argument("--schedule", default=None)
    p.set_defaults(func=_cmd_supervise)

    p = sub.add_parser("inspect", help="inspect a run from the Store")
    p.add_argument("run_id")
    p.add_argument("--dir", default=".", help="project directory")
    p.set_defaults(func=_cmd_inspect)

    p = sub.add_parser("logs", help="tail a run's events")
    p.add_argument("run_id")
    p.add_argument("--after", type=int, default=0, help="return events after this index")
    p.add_argument("--dir", default=".", help="project directory")
    p.set_defaults(func=_cmd_logs)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    result: int = args.func(args)
    return result


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
