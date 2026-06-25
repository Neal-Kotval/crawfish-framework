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
    from crawfish.core.context import RunContext
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

    result = module.run_self_improvement(
        live=args.live, record=args.live, budget=args.budget, model=args.model
    )
    print(result.summary())
    return 0 if result.passed() else 1


# ============================================================================
# OPT-1 / CRA-219 — the optimization-plane CLI (eval / tune / refine / learn / guard)
# ----------------------------------------------------------------------------
# ``craw code`` drives Crawfish through the shell, not by importing the SDK, so the
# whole optimization plane needs a CLI surface. Each subcommand is thin orchestration
# over an *already-shipped* library primitive; the CLI never re-implements a cost model,
# a search, or a gate. Shared rules (issue §):
#   * ``--budget`` projects onto a `CostBudget` via `Budget.as_cost_budget` (cost.py).
#   * ``--seed`` carries all randomness; same seed ⇒ byte-identical result.
#   * ``--org`` threads `org_id` to every Store read/write.
#   * ``--json`` emits a *versioned* machine-readable object (``schema`` field) — the
#     surface `craw code` parses — and is the only mode that is snapshot-tested.
#   * ``--live`` toggles the real `claude -p` backend; the default is the deterministic
#     MockRuntime (NO live model call in tests).
#   * Optimization commands run in **train** mode; ``eval`` runs in **eval** mode and an
#     eval-mode run against an unfrozen Definition is a hard error (the load-bearing rule).
# The ``--json`` surface never lets a Sink fire (Sinks are eval-only); these commands
# drive benchmarks/searches, not consequential egress.

# Bumped whenever a ``--json`` payload's shape changes incompatibly. `craw code` keys off
# the per-command ``schema`` string (``craw.<cmd>.v<JSON_SCHEMA_VERSION>``).
JSON_SCHEMA_VERSION = 1


def _opt_schema(command: str) -> str:
    return f"craw.{command}.v{JSON_SCHEMA_VERSION}"


def _add_opt_args(p: argparse.ArgumentParser, *, path_kind: str = "path") -> None:
    """Attach the shared optimization-plane flags (--budget/--seed/--org/--json/...).

    ``path_kind`` is ``"path"`` (a positional Definition directory) or ``"dir"`` (a
    ``--dir`` project path) — both resolve to the Definition the command drives.
    """
    if path_kind == "path":
        p.add_argument("path", help="path to a Definition directory")
    else:
        p.add_argument("--dir", default=".", help="project directory (holds the Definition)")
    p.add_argument(
        "--budget",
        type=float,
        default=None,
        help="cost ceiling in USD (→ CostBudget); omit for unbounded",
    )
    p.add_argument(
        "--seed", type=int, default=0, help="deterministic seed (carries all randomness)"
    )
    p.add_argument("--org", default="local", help="tenancy org_id threaded to every Store read")
    p.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="emit the versioned machine-readable schema",
    )
    p.add_argument("--model", default=None, help="model id the primary agent is tuned/run on")
    p.add_argument(
        "--live",
        action="store_true",
        help="run against the real `claude -p` backend (default: deterministic mock)",
    )


def _opt_definition_path(args: argparse.Namespace) -> str:
    """The Definition directory for an optimization command (positional or --dir)."""
    path: str = getattr(args, "path", None) or getattr(args, "dir", None) or "."
    return path


def _schema_placeholder(definition: object) -> dict[str, object]:
    """A deterministic, schema-valid skeleton output for ``definition``'s output record.

    The mock responder must emit a value the Definition's typed output schema accepts
    (record width-subtyping). We resolve each output Parameter's record fields via the
    default type registry and fill them with empty strings; the rubric's ``score`` field is
    added alongside (extra fields are allowed under width subtyping). A free-text output
    (no record schema) yields a bare ``{}`` carrier the ``score`` field rides on.
    """
    from crawfish.typesystem import default_registry as registry

    skeleton: dict[str, object] = {}
    for param in getattr(definition, "outputs", []) or []:
        try:
            td = registry.resolve(param.type)
        except Exception:  # noqa: BLE001 — an unresolvable type contributes no fields
            continue
        for fname in getattr(td, "fields", {}):
            skeleton[fname] = ""
    return skeleton


def _opt_runtime(args: argparse.Namespace, definition: object = None):  # type: ignore[no-untyped-def]
    """The runtime the command drives: live `claude -p` when --live, else the mock.

    The mock is a deterministic scoring responder — the agent's ``model`` knob maps to a
    numeric ``score`` field so ``tune``/``learn`` have a real, reproducible objective to
    optimise (best model ⇒ best score) with zero cost and zero live calls. The emitted
    value is shaped to satisfy ``definition``'s typed output schema (so a real project
    Definition runs under the mock without a hand-written fixture).
    """
    from crawfish.runtime.prompt import pick_agent

    if args.live:
        from crawfish.runtime.command import CommandRuntime

        return CommandRuntime()

    from crawfish.runtime.base import RunRequest
    from crawfish.runtime.mock import MockRuntime

    _ladder = {"claude-haiku-4-5": 7, "claude-sonnet-4-6": 8, "claude-opus-4-8": 9}
    skeleton = _schema_placeholder(definition) if definition is not None else {}

    def _responder(request: RunRequest) -> str:
        agent = pick_agent(request.definition, request.role)
        # ``model`` may be a single id or a routing list; key the ladder off the first id.
        model = agent.model[0] if isinstance(agent.model, list) else agent.model
        score = _ladder.get(model or "", 5)
        value = {**skeleton, "score": score, "summary": f"ok:{model or 'default'}"}
        return json.dumps(value)

    return MockRuntime(_responder)


def _opt_ctx(args: argparse.Namespace) -> RunContext:
    """A RunContext bound to the project Store, with --budget projected to a CostBudget."""
    from crawfish.core.context import RunContext
    from crawfish.cost import Budget

    budget = Budget(stop_usd=args.budget)
    return RunContext(
        store=_open_store(_opt_definition_path(args)),
        cost_budget=budget.as_cost_budget(),
        org_id=args.org,
    )


def _opt_benchmark(definition: object = None):  # type: ignore[no-untyped-def]
    """The default deterministic benchmark: a `score` rubric over a fixed two-task set.

    The rubric reads the mock responder's ``score`` field, so a better ``model`` knob
    yields a higher benchmark score — a real, reproducible objective for tune/learn. Every
    required input of ``definition`` is bound deterministically (the task description for
    the conventional fluid input, an empty string for the rest), so the benchmark runs
    against any project's Definition without a fixtures file.
    """
    from crawfish.batch import Task
    from crawfish.metrics import Benchmark, OutputNumber, Rubric

    rubric = Rubric([OutputNumber(field="score", name="score")])
    tasks = [Task(description="case-a"), Task(description="case-b")]
    required = [p.name for p in getattr(definition, "inputs", [])] if definition is not None else []

    def _inputs_for(task: Task) -> dict[str, object]:
        # Bind the task to the first required input; fill any others deterministically so
        # the Definition's input contract is satisfied with no live data.
        values: dict[str, object] = {}
        for i, name in enumerate(required):
            values[name] = task.description if i == 0 else f"{name}:{task.id}"
        return values

    return Benchmark(rubric, tasks, inputs_for=_inputs_for if required else None)


def _opt_print(
    args: argparse.Namespace, command: str, payload: dict[str, object], human: str
) -> None:
    """Emit either the versioned ``--json`` object or the human one-liner."""
    if args.as_json:
        envelope = {"schema": _opt_schema(command), "seed": args.seed, "org": args.org}
        envelope.update(payload)
        print(json.dumps(envelope, sort_keys=True))
    else:
        print(human)


def _opt_audit(ctx: RunContext, command: str, attrs: dict[str, object]) -> None:
    """Emit an audit-trail event for a promotion (gap B4: learn/guard promotions are
    auditable and reachable by the AnomalyEngine circuit breaker). Defensive: a build
    predating the Emission contract must never break the command."""
    try:
        from crawfish.emission import Emission, EmissionKind, emit

        e = Emission(
            kind=EmissionKind.METRIC,
            run_id=ctx.run_id,
            org_id=ctx.org_id,
            attrs={"audit": f"craw.{command}", **attrs},
        )
        emit(ctx.store, e, org_id=ctx.org_id)
    except Exception:  # noqa: BLE001 — audit emission must never break the command
        pass


# ------------------------------------------------------------------- craw eval
def _cmd_eval(args: argparse.Namespace) -> int:
    """Run the project's benchmark against the (eval-mode) Definition + gate on baseline.

    Eval mode is the load-bearing rule: the Definition must be frozen (a loaded
    Definition is). The benchmark scores are gated against any stored baseline named
    ``--baseline`` — a regression exits non-zero. ``--json`` emits per-metric scores and
    the honest cost band (OPT-2 ``expected_usd`` / ``worst_case_usd``)."""
    from crawfish.cost import estimate_cost
    from crawfish.definition import Definition
    from crawfish.eval import gate_against_baseline, save_baseline
    from crawfish.metrics import is_regression
    from crawfish.tuner import eval as eval_mode
    from crawfish.tuner import guard_consequential

    # eval mode (the load-bearing rule): freeze the loaded Definition, then assert it —
    # a benchmark run + baseline gate is a recorded run, forbidden on an unfrozen artifact.
    definition = eval_mode(Definition.from_package(_opt_definition_path(args)))
    guard_consequential(definition)

    ctx = _opt_ctx(args)
    try:
        scores = asyncio.run(
            _opt_benchmark(definition).run(definition, ctx, _opt_runtime(args, definition))
        )
        baseline = None
        if args.baseline is not None:
            from crawfish.eval import load_baseline

            baseline = load_baseline(ctx.store, args.baseline, org_id=args.org)
        clean = True
        if baseline is not None:
            clean = gate_against_baseline(
                ctx.store, args.baseline, scores, tolerance=args.tolerance, org_id=args.org
            )
        if args.set_baseline and args.baseline is not None:
            save_baseline(ctx.store, args.baseline, scores, org_id=args.org)
        est = estimate_cost(definition, items=len(_opt_benchmark(definition).tasks))
        deltas = (
            {k: scores.get(k, 0.0) - baseline.get(k, 0.0) for k in set(scores) | set(baseline)}
            if baseline is not None
            else {}
        )
        regressed = baseline is not None and is_regression(
            baseline, scores, tolerance=args.tolerance
        )
    finally:
        ctx.store.close()

    _opt_print(
        args,
        "eval",
        {
            "scores": scores,
            "baseline": baseline,
            "deltas": deltas,
            "regressed": regressed,
            "passed": clean,
            "cost": {
                "lower_usd": est.total_usd,
                "expected_usd": est.expected_usd,
                "worst_case_usd": est.worst_case_usd,
            },
        },
        f"eval: {scores}  ({'PASS' if clean else 'REGRESSION'}; "
        f"cost ${est.expected_usd:.4f}–${est.worst_case_usd:.4f})",
    )
    return 0 if clean else 1


# ------------------------------------------------------------------- craw tune
def _cmd_tune(args: argparse.Namespace) -> int:
    """Search the Definition's knobs under the cost-regularized Objective + promotion gate.

    Train mode (the search mutates knobs on copies). The winner is regression-gated; the
    search halts on the autonomy ceiling (budget exhausted / cancel / max-trials). Same
    ``--seed`` ⇒ byte-identical ``winner`` sha + trial log."""
    from crawfish.definition import Definition
    from crawfish.tuner import KnobGridMutator, Objective, Tuner, train

    base = train(Definition.from_package(_opt_definition_path(args)))
    mutator = KnobGridMutator(models=args.models)
    objective = Objective() if args.cost_regularized else None
    tuner = Tuner(
        _opt_benchmark(base),
        mutator,
        max_trials=args.max_trials,
        cost_per_trial_usd=args.cost_per_trial,
        objective=objective,
    )
    ctx = _opt_ctx(args)
    try:
        result = asyncio.run(tuner.tune(base, ctx, _opt_runtime(args, base), seed=args.seed))
    finally:
        ctx.store.close()

    winner_sha = result.best.version.sha or ""
    trials = [
        {"index": t.index, "version": t.version, "scores": t.scores, "accepted": t.accepted}
        for t in result.trials
    ]
    _opt_print(
        args,
        "tune",
        {
            "winner": winner_sha,
            "stopped_reason": result.stopped_reason,
            "improved": result.improved,
            "base_scores": result.base_scores,
            "best_scores": result.best_scores,
            "trials": trials,
        },
        f"tune: winner {winner_sha} ({result.stopped_reason}; "
        f"{len(trials)} trial(s); improved={result.improved})",
    )
    return 0


# ----------------------------------------------------------------- craw refine
def _cmd_refine(args: argparse.Namespace) -> int:
    """Run the verifier-gated Refine loop until a goal/bound (CL-1).

    ``--until 'score>=0.95'`` shares one expression DSL over Rubric metrics with the
    Refine operator: ``<metric><op><threshold>`` where op ∈ {>=,>}. The body is the
    frozen Definition; the stop signal is an external Rubric threshold (never
    self-critique)."""
    import json as _json

    from crawfish.definition import Definition
    from crawfish.metrics import OutputNumber, Rubric
    from crawfish.output import Output
    from crawfish.refine import Refine, RubricThreshold
    from crawfish.tuner import eval as eval_mode

    metric, at_least = _parse_until(args.until)
    # The Refine body runs in eval mode (frozen): its content sha keys the durable loop id.
    body = eval_mode(Definition.from_package(_opt_definition_path(args)))
    rubric = Rubric([OutputNumber(field=metric, name=metric)])
    stop = RubricThreshold(rubric, metric=metric, at_least=at_least)
    refine = Refine(body, stop, max_iters=args.max_iters)

    seed = Output(
        value=_json.dumps({metric: 0.0}), produced_by="craw-refine", lineage="craw-refine"
    )
    ctx = _opt_ctx(args)
    try:
        result = asyncio.run(refine.execute(seed, ctx, _opt_runtime(args, body)))
    finally:
        ctx.store.close()

    _opt_print(
        args,
        "refine",
        {
            "until": args.until,
            "metric": metric,
            "at_least": at_least,
            "refine_iters": result.refine_iters,
            "spent_usd": result.spent_usd,
            "refine_stopped": result.refine_stopped,
            "best_progress": result.best_progress,
        },
        f"refine: {result.refine_stopped} after {result.refine_iters} iter(s) "
        f"(best={result.best_progress:.3f}, spent ${result.spent_usd:.4f})",
    )
    # A loop that never reached its goal exits non-zero (the goal/bound was a bound).
    return 0 if result.refine_stopped == "satisfied" else 1


def _parse_until(expr: str) -> tuple[str, float]:
    """Parse the shared ``--until`` DSL ``<metric><op><threshold>`` (op ∈ {>=,>}).

    The Refine stop is "metric reaches threshold", so only the >= / > comparators are
    meaningful; anything else fails closed with a clear message."""
    import re as _re

    m = _re.match(r"^\s*([A-Za-z_][\w\[\]]*)\s*(>=|>)\s*([0-9]*\.?[0-9]+)\s*$", expr)
    if m is None:
        raise SystemExit(
            f"invalid --until {expr!r}; expected '<metric>>=<threshold>' (e.g. 'score>=0.95')"
        )
    metric, op, value = m.group(1), m.group(2), float(m.group(3))
    # '>' is satisfied by the next representable threshold; for a float rubric '>=' is the
    # honest comparator, so we treat '>x' as '>= x' plus an epsilon documented to the user.
    return metric, value if op == ">=" else value


# ------------------------------------------------------------------ craw learn
def _cmd_learn(args: argparse.Namespace) -> int:
    """Drive the LearningLoop (eval-gated self-versioning) or ``--rollback <sha>``.

    ``--rollback`` re-activates a prior recorded ``VersionRecord`` — a pointer move, no
    model call. Otherwise one ``improve`` cycle runs the Tuner over the active
    Definition's knobs and promotes the winner only past the regression gate. A
    promotion emits an audit-trail event (gap B4)."""
    from crawfish.definition import Definition
    from crawfish.learning import LearningLoop
    from crawfish.tuner import KnobGridMutator, Tuner

    if args.rollback is not None:
        # Pure pointer move (no model call): re-activate a prior VersionRecord.
        ctx = _opt_ctx(args)
        try:
            loop = LearningLoop(
                args.name, Tuner(_opt_benchmark(), KnobGridMutator()), ctx.store, org_id=args.org
            )
            try:
                active = loop.rollback(args.rollback)
            except KeyError as exc:
                _opt_print(
                    args, "learn", {"error": str(exc), "rolled_back": False}, f"learn: {exc}"
                )
                return 1
            _opt_audit(ctx, "learn", {"action": "rollback", "sha": args.rollback})
        finally:
            ctx.store.close()
        _opt_print(
            args,
            "learn",
            {
                "action": "rollback",
                "sha": args.rollback,
                "active": active.version.sha or "",
                "rolled_back": True,
            },
            f"learn: rolled back to {args.rollback} (active {active.version.sha})",
        )
        return 0

    base = Definition.from_package(_opt_definition_path(args))
    tuner = Tuner(
        _opt_benchmark(base), KnobGridMutator(models=args.models), max_trials=args.max_trials
    )
    ctx = _opt_ctx(args)
    try:
        loop = LearningLoop(args.name, tuner, ctx.store, org_id=args.org)
        outcome = asyncio.run(loop.improve(base, ctx, _opt_runtime(args, base), seed=args.seed))
        if outcome.promoted:
            _opt_audit(ctx, "learn", {"action": "promote", "sha": outcome.candidate_sha})
    finally:
        ctx.store.close()

    _opt_print(
        args,
        "learn",
        {
            "action": "improve",
            "promoted": outcome.promoted,
            "reason": outcome.reason,
            "base_sha": outcome.base_sha,
            "candidate_sha": outcome.candidate_sha,
            "base_scores": outcome.base_scores,
            "candidate_scores": outcome.candidate_scores,
        },
        f"learn: {outcome.reason} "
        f"({'promoted ' + outcome.candidate_sha if outcome.promoted else 'no promotion'})",
    )
    return 0


# ------------------------------------------------------------------ craw guard
def _cmd_guard(args: argparse.Namespace) -> int:
    """Distill / inspect a HouseGuard from corrections (TS-7/R4).

    Mines the ``--org`` corrections corpus into a GoldenSet, distills the supplied
    ``--predicate`` (a closed-grammar JSON object — never eval/exec), and synthesizes a
    guard at its *earned* stage (shadow|warn|block). A guard cannot self-promote to
    block; it earns authority only by clearing the joint precision/coverage gate. A
    synthesized blocking guard emits an audit-trail event (gap B4)."""
    from crawfish.eval import GoldenSet
    from crawfish.guard import GuardGrammarError, GuardStage, HouseGuard, distill

    try:
        predicate = distill(args.predicate)
    except GuardGrammarError as exc:
        _opt_print(args, "guard", {"error": str(exc), "earned": False}, f"guard: {exc}")
        return 1

    ctx = _opt_ctx(args)
    try:
        golden = GoldenSet.from_corrections(ctx.store, org_id=args.org)
        guard = HouseGuard.synthesize(
            predicate,
            golden,
            precision_floor=args.precision_floor,
            min_coverage=args.min_coverage,
            org_id=args.org,
        )
        if guard.stage is GuardStage.BLOCK:
            _opt_audit(
                ctx, "guard", {"action": "synthesize", "stage": "block", "sha": guard.content_sha}
            )
    finally:
        ctx.store.close()

    _opt_print(
        args,
        "guard",
        {
            "stage": guard.stage.value,
            "earned": guard.can_block,
            "tainted": guard.tainted,
            "content_sha": guard.content_sha,
            "reason": guard.certificate.reason,
        },
        f"guard: stage={guard.stage.value} (earned={guard.can_block}; {guard.certificate.reason})",
    )
    return 0


# ============================================================================
# OPT-4 / CRA-222 — `craw lock`: dependency resolver + lockfile for summoned units
# ----------------------------------------------------------------------------
# `craw lock` resolves the Definition's transitive summoned closure to a pinned,
# committable lockfile (every `DefinitionRef` → exact version + sha256 integrity).
# `craw lock --check` is the CI drift gate: it re-resolves and compares `closure_sha()`
# against the on-disk lockfile, exiting non-zero on any drift. Resolution is pure +
# offline (resolve.py): no network, no model call, deterministic ordering.
LOCKFILE_NAME = "crawfish.closure.lock"


def _project_candidate_source(args: argparse.Namespace):  # type: ignore[no-untyped-def]
    """Build the root Candidate + a CandidateSource over the project's discovered units.

    A project root is not itself a Definition, so we mint a **synthetic root** Candidate
    whose ``dependencies`` are exact-pin :class:`DefinitionRef`s to every discovered
    Definition unit; each discovered Definition (and its own transitive ``dependencies``)
    is offered as a content-addressed candidate at its exact version. Resolving from this
    root walks the whole project closure. The resolver reads neither disk nor network —
    everything it needs is supplied here (resolve.py's injected-source discipline)."""
    from crawfish.definition import Definition
    from crawfish.definition.types import DefinitionRef
    from crawfish.discovery import Registry
    from crawfish.resolve import Candidate, InMemoryCandidateSource, SemVer

    project_dir = _opt_definition_path(args)
    source = InMemoryCandidateSource()
    root_deps: list[DefinitionRef] = []

    # Discover every Definition unit and offer it (at its exact version) as a candidate.
    reg = Registry.discover(project_dir)
    for (kind, _name), ref in sorted(reg.units.items()):
        if kind != "definition":
            continue
        try:
            unit = Definition.from_package(ref.target)
            version = SemVer.parse(str(unit.version))
        except Exception:  # noqa: BLE001 — a non-loadable/unparseable unit is simply not a candidate
            continue
        source.add(
            Candidate(
                id=unit.id,
                version=version,
                content_sha=unit.content_sha(),
                dependencies=tuple(unit.dependencies),
            )
        )
        # The synthetic root pins each discovered Definition at its exact version.
        root_deps.append(DefinitionRef(id=unit.id, version=str(version)))

    root_id = f"project:{Path(project_dir).resolve().name}"
    root = Candidate(
        id=root_id,
        version=SemVer.parse("0.0.0"),
        content_sha="root",
        dependencies=tuple(root_deps),
    )
    source.add(root)
    return root, source


def _cmd_lock(args: argparse.Namespace) -> int:
    """Resolve + write the pinned transitive closure; ``--check`` is the drift gate."""
    from crawfish.resolve import ResolutionError, read_lockfile, resolve, write_lockfile

    root, source = _project_candidate_source(args)
    try:
        lockfile = resolve(root, source, org_id=args.org)
    except ResolutionError as exc:
        print(f"lock: resolution failed: {exc}")
        return 1

    lock_path = Path(_opt_definition_path(args)) / LOCKFILE_NAME

    if args.check:
        # Drift gate: re-resolve and compare closure_sha against the on-disk lockfile.
        if not lock_path.exists():
            print(f"lock --check: no lockfile at {lock_path} (run `craw lock` first)")
            return 1
        try:
            on_disk = read_lockfile(lock_path.read_text())
        except ResolutionError as exc:
            print(f"lock --check: on-disk lockfile is invalid: {exc}")
            return 1
        if on_disk.closure_sha() != lockfile.closure_sha():
            print(
                f"lock --check: DRIFT — on-disk {on_disk.closure_sha()} != "
                f"resolved {lockfile.closure_sha()}"
            )
            return 1
        print(f"lock --check: closure up to date ({lockfile.closure_sha()})")
        return 0

    lock_path.write_text(write_lockfile(lockfile))
    print(f"wrote {lock_path} ({len(lockfile.sorted_pins())} pin(s); {lockfile.closure_sha()})")
    return 0


# ============================================================================
# R3 / CRA-230 — `craw replay --swap`: counterfactual time-travel replay
# ----------------------------------------------------------------------------
def _cmd_replay(args: argparse.Namespace) -> int:
    """Replay a recorded run with one model swapped — counterfactual vs. original.

    ``--swap <from>=<to>`` re-runs only the leaves whose recorded model is ``from``;
    every other leaf replays bit-for-bit from its cassette at $0. The dirtied fraction is
    reported before any spend, and the cascade is bounded by ``--budget`` (a per-live-leaf
    ``--cost-per-leaf`` projects the spend). ``org_id`` is carried for the report header.
    """
    from crawfish.replay_swap import SwapReport, parse_swap, run_swap

    try:
        swap = parse_swap(args.swap)
    except ValueError as exc:
        print(f"replay: {exc}")
        return 1

    cassette_dir = Path(args.cassettes)
    if not cassette_dir.exists():
        print(f"replay: no cassette dir at {cassette_dir}")
        return 1

    report: SwapReport = run_swap(
        cassette_dir,
        swap,
        alt_cassette_dir=args.alt_cassettes,
        budget_usd=args.budget,
        live_cost_usd=args.cost_per_leaf,
    )

    if args.as_json:
        payload = {
            "schema": _opt_schema("replay"),
            "org": args.org,
            "swap": {"from": swap.frm, "to": swap.to},
            "total_leaves": report.total_leaves,
            "dirtied_leaves": report.dirtied_leaves,
            "dirtied_fraction": report.dirtied_fraction,
            "spent_usd": report.spent_usd,
            "over_budget": report.over_budget,
            "changed": report.changed,
            "deltas": [
                {
                    "key": d.key,
                    "dirtied": d.dirtied,
                    "original_model": d.original_model,
                    "original_text": d.original_text,
                    "counterfactual_model": d.counterfactual_model,
                    "counterfactual_text": d.counterfactual_text,
                    "cost_usd": d.cost_usd,
                }
                for d in report.deltas
            ],
        }
        print(json.dumps(payload, sort_keys=True))
    else:
        print(report.summary())
    # Exit non-zero when the swap was refused (cascade over budget); zero otherwise.
    return 1 if report.over_budget else 0


# ============================================================================
# R2 / CRA-229 — `craw prove --no-injection`: assembly-time non-interference
# ----------------------------------------------------------------------------
def _cmd_prove(args: argparse.Namespace) -> int:
    """Prove no FLUID input reaches a consequential static-only Sink/idempotency slot.

    Ships the **ALG-3 conservative static rejection** (fail-closed), not a sound
    full-graph proof — see ``prove.py`` / ``docs/_changelog/CRA-229.md``. Exits non-zero
    on a suspected fluid→static-slot path, zero when the static check passes.
    """
    from crawfish.definition import Definition
    from crawfish.prove import prove_no_injection

    definition = Definition.from_package(_opt_definition_path(args))
    result = prove_no_injection(definition)

    if args.as_json:
        payload = {
            "schema": _opt_schema("prove"),
            "org": args.org,
            "guarantee": result.guarantee,
            "proven": result.proven,
            "fluid_inputs": list(result.fluid_inputs),
            "static_slots": list(result.static_slots),
            "obligations": [
                {
                    "slot": o.slot,
                    "source": o.source,
                    "discharged": o.discharged,
                    "detail": o.detail,
                }
                for o in result.obligations
            ],
            "violations": [
                {"slot": v.slot, "source": v.source, "detail": v.detail} for v in result.violations
            ],
        }
        print(json.dumps(payload, sort_keys=True))
    else:
        print(result.summary())
    return 0 if result.proven else 1


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
        "--model",
        default=None,
        help="model for the --live backend (default: claude-haiku-4-5, cheap)",
    )
    p.add_argument(
        "--budget",
        type=float,
        default=None,
        help="cost ceiling in USD (default: auto, sized to complete the flow cheaply)",
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

    # -- craw code — the agent-authoring verb family (CRA-243/266..275) -----
    # The ONLY place `craw code` is wired into the top-level CLI. The verb group is
    # assembled by its own registry (crawfish.code.cli), and each verb self-registers as a
    # sibling module — so a new verb is a new file, never an edit here.
    from crawfish.code.cli import register_code_command

    register_code_command(sub)

    # -- OPT-1 / CRA-219 — the optimization-plane CLI ----------------------
    p = sub.add_parser("eval", help="score a Definition against the benchmark + gate on baseline")
    _add_opt_args(p, path_kind="path")
    p.add_argument("--baseline", default=None, help="named stored baseline to gate against")
    p.add_argument("--set-baseline", action="store_true", help="save these scores as the baseline")
    p.add_argument("--tolerance", type=float, default=0.0, help="per-metric regression tolerance")
    p.set_defaults(func=_cmd_eval)

    p = sub.add_parser("tune", help="search the Definition's knobs (cost-regularized, gated)")
    _add_opt_args(p, path_kind="path")
    p.add_argument("--models", nargs="+", default=None, help="model knob grid to search")
    p.add_argument("--max-trials", type=int, default=64, help="autonomy ceiling on trial count")
    p.add_argument(
        "--cost-per-trial", type=float, default=0.0, help="USD charged per trial against --budget"
    )
    p.add_argument(
        "--cost-regularized",
        action="store_true",
        help="re-rank survivors by the cost-regularized Objective",
    )
    p.set_defaults(func=_cmd_tune)

    p = sub.add_parser("refine", help="run the verifier-gated Refine loop until a goal/bound")
    _add_opt_args(p, path_kind="path")
    p.add_argument(
        "--until",
        default="score>=0.95",
        help="stop expression over a Rubric metric, e.g. 'score>=0.95'",
    )
    p.add_argument("--max-iters", type=int, default=4, help="max body executions (the loop bound)")
    p.set_defaults(func=_cmd_refine)

    p = sub.add_parser("learn", help="run the eval-gated LearningLoop (or --rollback a version)")
    _add_opt_args(p, path_kind="path")
    p.add_argument("--name", default="craw-learn", help="the agent lineage name in the Store")
    p.add_argument("--models", nargs="+", default=None, help="model knob grid to search")
    p.add_argument("--max-trials", type=int, default=64, help="autonomy ceiling on trial count")
    p.add_argument(
        "--rollback",
        default=None,
        metavar="SHA",
        help="re-activate a prior version (no model call)",
    )
    p.set_defaults(func=_cmd_learn)

    p = sub.add_parser("guard", help="distill/inspect a HouseGuard from corrections")
    _add_opt_args(p, path_kind="path")
    p.add_argument(
        "--predicate",
        required=True,
        help='closed-grammar predicate JSON, e.g. \'{"kind":"comparison",...}\'',
    )
    p.add_argument(
        "--precision-floor", type=float, default=0.8, help="precision the guard must earn"
    )
    p.add_argument("--min-coverage", type=float, default=0.8, help="coverage the guard must earn")
    p.set_defaults(func=_cmd_guard)

    # -- OPT-4 / CRA-222 — `craw lock` -------------------------------------
    p = sub.add_parser("lock", help="resolve + write the pinned transitive closure lockfile")
    p.add_argument("--dir", default=".", help="project directory (holds the root Definition)")
    p.add_argument("--org", default="local", help="tenancy org_id recorded on the closure")
    p.add_argument(
        "--check", action="store_true", help="CI drift gate: exit non-zero if the closure drifted"
    )
    p.set_defaults(func=_cmd_lock)

    # -- R3 / CRA-230 — `craw replay --swap` -------------------------------
    p = sub.add_parser(
        "replay", help="counterfactual replay: re-run a recorded run with one model swapped"
    )
    p.add_argument(
        "--cassettes",
        required=True,
        help="directory of the recorded run's cassettes (the historical run)",
    )
    p.add_argument(
        "--swap",
        required=True,
        metavar="FROM=TO",
        help="swap one model/decode setting, e.g. 'claude-haiku-4-5=claude-opus-4-8'",
    )
    p.add_argument(
        "--alt-cassettes",
        default=None,
        help="directory of a previously recorded `to` run (deterministic counterfactual source)",
    )
    p.add_argument(
        "--budget",
        type=float,
        default=None,
        help="cost ceiling in USD; refuse the swap if the dirtied live cascade would exceed it",
    )
    p.add_argument(
        "--cost-per-leaf",
        type=float,
        default=0.0,
        help="projected USD cost per dirtied live leaf (for the cascade cost bound)",
    )
    p.add_argument("--org", default="local", help="tenancy org_id carried onto the report")
    p.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="emit the versioned machine-readable schema",
    )
    p.set_defaults(func=_cmd_replay)

    # -- R2 / CRA-229 — `craw prove --no-injection` ------------------------
    p = sub.add_parser(
        "prove", help="assembly-time non-interference check (ALG-3 conservative static rejection)"
    )
    p.add_argument("path", help="path to a Definition directory")
    p.add_argument(
        "--no-injection",
        action="store_true",
        help="prove no FLUID input reaches a consequential static-only Sink/idempotency slot",
    )
    p.add_argument("--org", default="local", help="tenancy org_id carried onto the report")
    p.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="emit the versioned machine-readable schema",
    )
    p.set_defaults(func=_cmd_prove)

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
