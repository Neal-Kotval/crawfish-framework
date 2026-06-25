"""``craw code optimize <component>`` — the tune / refine / learn orchestrator.

The operate plane's *optimization* verb (UNFILED-OPTIMIZE, M4.5). It is a thin driver that
**composes the shipped optimization engines** (:class:`crawfish.tuner.Tuner`,
:class:`crawfish.refine.Refine`, :mod:`crawfish.eval`, :class:`crawfish.learning.Tuner`-backed
search) — it reinvents none of them. One pass:

1. **Scaffold** ``tune.toml`` (the :class:`~crawfish.tune.TuneSpec` authored form) for the
   component when absent, from a reference-only template (no inline secrets / destinations —
   CRA-276). An existing ``tune.toml`` is never clobbered.
2. **Seed a baseline** via the eval path (``save_baseline`` over an eval-mode benchmark run)
   so the F-3 promotion gate has a real regression baseline to compare against.
3. **Drive the mode-appropriate inner loop** under ``--budget`` (a shipped
   :class:`~crawfish.core.context.CostBudget` ceiling): ``tune`` over a knob space,
   ``refine`` toward a Rubric goal, ``learn`` to search the agent's own knobs. ``--mode
   auto`` inspects the component (knob space present → tune; Rubric present → refine) and
   reports the choice.
4. **Emit** the ``craw.code.optimize.v1`` summary: winner sha, per-metric deltas vs the
   baseline, ``stopped_reason``, ``spent_usd``.

**This verb does NOT auto-promote.** It runs eval-mode/frozen, fires no Sink, and *proposes*
a winner (``promoted`` is always ``False`` here). Promotion to the live/active version is
the human gate's job (M6 ``craw code propose``/``apply``); the winner is a generated
artifact that must pass the assembly gate before it can ship. Determinism: a fixed
``--seed`` ⇒ identical winner + trial order (no live model call; a deterministic mock
runtime drives the search).
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any

from crawfish.code import (
    EXIT_OK,
    SCHEMA_VERSIONS,
    ErrorCode,
    emit_error,
    emit_json,
)

if TYPE_CHECKING:
    from crawfish.definition import Definition
    from crawfish.metrics import Benchmark
    from crawfish.runtime.base import AgentRuntime
    from crawfish.store.base import Store

SCHEMA_VERSIONS.setdefault("code.optimize", (1, 0))  # type: ignore[attr-defined]

VERB_NAME = "optimize"

#: ``optimize`` exit codes (the closed table over the CRA-243 base).
EXIT_OVER_BUDGET = 4  # over budget before any trial could run
EXIT_NO_BASELINE = 5  # no baseline could be seeded (the gate has nothing to compare to)

#: A reference-only ``tune.toml`` seed — declares a model knob domain, no secret/destination.
_TUNE_TOML_TEMPLATE = (
    "# Scaffolded by `craw code optimize`. The tunable knob space (the typed form of\n"
    "# crawfish.tune.TuneSpec). It is STATIC author config — it never reads a fluid value\n"
    "# and carries no secret or sink destination (CRA-276).\n"
    "[[knob]]\n"
    'path = "agent.lead.model"\n'
    'values = ["claude-haiku-4-5", "claude-sonnet-4-6", "claude-opus-4-8"]\n'
    "tunable = true\n"
)


class NoBaselineError(RuntimeError):
    """A regression baseline could not be seeded (exit 5)."""


def register(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    """Register ``craw code optimize`` on the ``code`` subparser group (self-registering)."""
    from crawfish.code.cli import add_common_args

    p = subparsers.add_parser(
        "optimize", help="orchestrate tune/refine/learn over a component (UNFILED-OPTIMIZE)"
    )
    p.add_argument("component", help="path to the component directory (e.g. definitions/triage)")
    p.add_argument(
        "--mode",
        choices=("auto", "tune", "refine", "learn"),
        default="auto",
        help="which inner loop to drive (auto inspects the component)",
    )
    p.add_argument("--budget", type=float, default=None, help="cost ceiling for the pass (USD)")
    p.add_argument("--seed", type=int, default=0, help="search seed (determinism)")
    p.add_argument("--until", default="score>=0.95", help="refine goal expression (refine mode)")
    p.add_argument("--max-trials", type=int, default=64, help="tune/learn search ceiling")
    p.add_argument(
        "--live",
        action="store_true",
        help="drive the real backend (default: deterministic mock — no live call)",
    )
    add_common_args(p)
    p.set_defaults(func=_cmd_optimize)


# --------------------------------------------------------------------------- mode selection


def select_mode(definition: Definition) -> str:
    """Pick the inner loop for ``--mode auto``: knob space → tune, Rubric goal → refine.

    A knob space is present when the component declares a non-empty tunable ``tune`` spec
    (``tune.toml``) or its primary agent carries a routing ``model`` list to search. A
    Rubric goal is the fallback signal for ``refine``. With neither clear signal we default
    to ``tune`` (the safest eval-mode search). Pure + deterministic.
    """
    tune = getattr(definition, "tune", None)
    if tune is not None and any(True for _ in tune.named_knobs()):
        return "tune"
    for agent in definition.team.agents:
        if isinstance(agent.model, list) and len(agent.model) > 1:
            return "tune"
    # No tunable knob space — a Rubric-goal component refines toward its bound.
    return "refine"


def _model_knob_space(definition: Definition) -> list[str] | None:
    """The primary agent's searchable ``model`` list, or ``None`` when there is no grid.

    The tune search axis: a ``model`` knob authored as a list of choices. Returns ``None``
    when the agent pins a single model (no grid to search) so the caller leaves that axis
    unset. Pure; reads only the loaded spec.
    """
    from crawfish.tuner import _primary_agent

    try:
        agent = _primary_agent(definition)
    except ValueError:
        return None
    if isinstance(agent.model, list) and len(agent.model) > 1:
        return list(agent.model)
    return None


# --------------------------------------------------------------------------- scaffolding


def scaffold_tune_toml(component: str | Path) -> bool:
    """Write a reference-only ``tune.toml`` iff absent. Returns True iff it scaffolded one.

    Never clobbers an existing ``tune.toml`` (the AC). The template declares a model knob
    domain only — STATIC author config, no inline secret or destination (CRA-276).
    """
    path = Path(component) / "tune.toml"
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_TUNE_TOML_TEMPLATE)
    return True


# --------------------------------------------------------------------------- the pass


def optimize_component(
    component: str,
    *,
    store: Store,
    mode: str = "auto",
    budget_usd: float | None = None,
    seed: int = 0,
    until: str = "score>=0.95",
    max_trials: int = 64,
    org_id: str = "local",
    runtime: AgentRuntime | None = None,
    benchmark: Benchmark | None = None,
) -> dict[str, object]:
    """Run one optimization pass and return the ``craw.code.optimize.v1`` body (no promotion).

    Composes the shipped engines under a :class:`CostBudget` ``--budget`` ceiling:

    * eval-mode benchmark run → ``save_baseline`` (so the promotion gate has a baseline);
    * ``tune``/``learn`` → :class:`~crawfish.tuner.Tuner` search for a winner;
    * ``refine`` → :class:`~crawfish.refine.Refine` toward the ``--until`` Rubric bound.

    The winner is **proposed, never promoted**: the returned ``promoted`` is always
    ``False`` and the active/live version pointer is untouched (M6's human gate promotes).
    No Sink fires (eval-mode/frozen). Same ``seed`` ⇒ same winner. ``runtime``/``benchmark``
    are injectable for deterministic tests; the defaults are a deterministic mock (no live
    call). Raises :class:`NoBaselineError` when no baseline can be seeded.
    """
    from crawfish.core.context import RunContext
    from crawfish.cost import Budget
    from crawfish.definition import Definition
    from crawfish.eval import load_baseline, save_baseline
    from crawfish.tuner import eval as eval_mode

    # Eval mode (the load-bearing rule): freeze the loaded Definition. A benchmark run is a
    # recorded run, forbidden on an unfrozen artifact; freezing also makes the winner a
    # generated artifact the assembly gate can verify before any future promotion.
    definition = eval_mode(Definition.from_package(component))
    runtime = runtime if runtime is not None else _default_runtime(definition)
    benchmark = benchmark if benchmark is not None else _default_benchmark(definition)

    chosen = select_mode(definition) if mode == "auto" else mode

    # (1) scaffold tune.toml when absent (reference-only; never clobbers).
    scaffolded = scaffold_tune_toml(component)

    budget = Budget(stop_usd=budget_usd)
    ctx = RunContext(store=store, cost_budget=budget.as_cost_budget(), org_id=org_id)
    baseline_name = f"optimize:{Path(component).name}"

    # (2) seed the regression baseline from an eval-mode benchmark run.
    base_scores = asyncio.run(benchmark.run(definition, ctx, runtime))
    if not base_scores:
        raise NoBaselineError(component)
    if load_baseline(store, baseline_name, org_id=org_id) is None:
        save_baseline(store, baseline_name, base_scores, org_id=org_id)
    baseline = load_baseline(store, baseline_name, org_id=org_id) or base_scores

    # (3) drive the mode-appropriate inner loop under the budget ceiling.
    if chosen == "refine":
        result = _drive_refine(definition, ctx, runtime, until=until, max_iters=max_trials)
    else:  # tune | learn — both search the knob space; neither promotes here.
        result = _drive_tune(definition, ctx, runtime, benchmark, seed=seed, max_trials=max_trials)

    deltas = {
        k: round(result["winner_scores"].get(k, 0.0) - baseline.get(k, 0.0), 6)
        for k in set(result["winner_scores"]) | set(baseline)
    }
    return {
        "component": component,
        "mode": chosen,
        "winner_sha": result["winner_sha"],
        # PROPOSAL, NOT PROMOTION: optimize never flips the active version (M6 gate does).
        "promoted": False,
        "metric_deltas": deltas,
        "stopped_reason": result["stopped_reason"],
        "spent_usd": round(ctx.cost_budget.spent_usd, 6),
        "baseline_sha": result["base_sha"],
        "tune_toml_scaffolded": scaffolded,
    }


def _drive_tune(
    definition: Definition,
    ctx: Any,
    runtime: AgentRuntime,
    benchmark: Benchmark,
    *,
    seed: int,
    max_trials: int,
) -> dict[str, Any]:
    """Search the knob space for a winner via the shipped Tuner (proposes; never promotes)."""
    from crawfish.tuner import KnobGridMutator, Tuner, train

    # Train mode lets the search mutate knobs on copies; the loaded Definition is the base.
    base = train(definition)
    # Seed the grid from the component's OWN searchable model knob space (the same signal
    # ``select_mode`` keyed on) so the search actually explores it — an empty KnobGridMutator
    # proposes zero candidates and the winner collapses to the base.
    mutator = KnobGridMutator(models=_model_knob_space(definition))
    tuner = Tuner(benchmark, mutator, max_trials=max_trials)
    result = asyncio.run(tuner.tune(base, ctx, runtime, seed=seed))
    return {
        "winner_sha": str(result.best.version.sha or ""),
        "winner_scores": dict(result.best_scores),
        "base_sha": str(base.version.sha or ""),
        "base_scores": dict(result.base_scores),
        "stopped_reason": result.stopped_reason,
    }


def _drive_refine(
    definition: Definition,
    ctx: Any,
    runtime: AgentRuntime,
    *,
    until: str,
    max_iters: int = 64,
) -> dict[str, Any]:
    """Refine toward the ``--until`` Rubric bound via the shipped Refine loop."""
    import json as _json

    from crawfish.metrics import OutputNumber, Rubric
    from crawfish.output import Output
    from crawfish.refine import Refine, RubricThreshold

    metric, at_least = _parse_until(until)
    rubric = Rubric([OutputNumber(field=metric, name=metric)])
    stop = RubricThreshold(rubric, metric=metric, at_least=at_least)
    refine = Refine(definition, stop, max_iters=max_iters)
    seed_out = Output(
        value=_json.dumps({metric: 0.0}), produced_by="craw-code-optimize", lineage="optimize"
    )
    result = asyncio.run(refine.execute(seed_out, ctx, runtime))
    return {
        # Refine improves an Output, not a Definition version — the frozen body sha is stable.
        "winner_sha": str(definition.version.sha or ""),
        "winner_scores": {metric: result.best_progress},
        "base_sha": str(definition.version.sha or ""),
        "base_scores": {metric: 0.0},
        "stopped_reason": (
            "satisfied" if result.refine_stopped == "satisfied" else result.refine_stopped
        ),
    }


def _parse_until(expr: str) -> tuple[str, float]:
    """Parse the ``--until`` DSL ``<metric><op><threshold>`` (op ∈ {>=,>}); fail closed."""
    import re

    m = re.match(r"^\s*([A-Za-z_][\w\[\]]*)\s*(>=|>)\s*([0-9]*\.?[0-9]+)\s*$", expr)
    if m is None:
        raise ValueError(f"invalid --until {expr!r}; expected '<metric>>=<threshold>'")
    return m.group(1), float(m.group(3))


# --------------------------------------------------------------------------- CLI glue


def _cmd_optimize(args: argparse.Namespace) -> int:
    """``craw code optimize <component> [--mode auto] [--budget B] [--seed N] [--json]``."""
    org = getattr(args, "org", "local")
    as_json = getattr(args, "as_json", False)
    if not Path(args.component).is_dir():
        return emit_error(
            ErrorCode.NOT_FOUND,
            remediation=f"Component {args.component!r} not found; pass a component directory.",
            detail={"component": args.component},
            as_json=as_json,
        )
    store = _store_for(args.component)
    try:
        body = optimize_component(
            args.component,
            store=store,
            mode=args.mode,
            budget_usd=args.budget,
            seed=args.seed,
            until=args.until,
            max_trials=args.max_trials,
            org_id=org,
            runtime=_default_runtime_for_args(args),
        )
    except NoBaselineError:
        emit_error(
            ErrorCode.INTERNAL,
            remediation="No baseline could be seeded; the benchmark produced no scores.",
            detail={"component": args.component},
            as_json=as_json,
        )
        return EXIT_NO_BASELINE
    finally:
        store.close()

    if as_json:
        emit_json("code.optimize", body, org=org)
    else:
        _print_human(body)
    # Over budget before any trial ran is the responsibility gate (exit 4) — surfaced from
    # the engine's ``stopped_reason`` rather than a stack trace.
    if body["stopped_reason"] == "budget" and not body["metric_deltas"]:
        return EXIT_OVER_BUDGET
    return EXIT_OK


def _default_runtime_for_args(args: argparse.Namespace) -> AgentRuntime | None:
    """The CLI runtime: live backend on ``--live``, else None (the deterministic mock)."""
    if getattr(args, "live", False):
        from crawfish.runtime.command import CommandRuntime

        return CommandRuntime()
    return None


def _default_runtime(definition: Definition) -> AgentRuntime:
    """A deterministic scoring mock — ``model`` knob → numeric score (no live call).

    Mirrors the optimization plane's mock responder so tune/learn have a real, reproducible
    objective (better model ⇒ higher score) at zero cost. The emitted value is shaped to
    satisfy the Definition's typed output schema.
    """
    import json as _json

    from crawfish.runtime.base import RunRequest
    from crawfish.runtime.mock import MockRuntime
    from crawfish.runtime.prompt import pick_agent

    ladder = {"claude-haiku-4-5": 7, "claude-sonnet-4-6": 8, "claude-opus-4-8": 9}
    skeleton = _schema_placeholder(definition)

    def _responder(request: RunRequest) -> str:
        agent = pick_agent(request.definition, request.role)
        model = agent.model[0] if isinstance(agent.model, list) else agent.model
        score = ladder.get(model or "", 5)
        return _json.dumps({**skeleton, "score": score, "summary": f"ok:{model or 'default'}"})

    return MockRuntime(_responder)


def _schema_placeholder(definition: Definition) -> dict[str, object]:
    """A deterministic, schema-valid skeleton for the Definition's output record."""
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


def _default_benchmark(definition: Definition) -> Benchmark:
    """The default deterministic benchmark: a ``score`` rubric over a fixed two-task set."""
    from crawfish.batch import Task
    from crawfish.metrics import Benchmark, OutputNumber, Rubric

    rubric = Rubric([OutputNumber(field="score", name="score")])
    tasks = [Task(description="case-a"), Task(description="case-b")]
    required = [p.name for p in getattr(definition, "inputs", [])]

    def _inputs_for(task: Task) -> dict[str, object]:
        values: dict[str, object] = {}
        for i, name in enumerate(required):
            values[name] = task.description if i == 0 else f"{name}:{task.id}"
        return values

    return Benchmark(rubric, tasks, inputs_for=_inputs_for if required else None)


def _store_for(component: str) -> Store:
    """Open the per-project Store through the protocol-returning factory (never a backend)."""
    from crawfish.manage import store_for_dir

    Path(component, ".crawfish").mkdir(parents=True, exist_ok=True)
    return store_for_dir(component)


def _print_human(body: dict[str, object]) -> None:
    """Human one-liner for the optimize summary (proposes a winner; never promotes)."""
    print(
        f"optimize {body['component']} [{body['mode']}]: winner {body['winner_sha']} "
        f"(proposed, not promoted; {body['stopped_reason']}; "
        f"spent ${body['spent_usd']:.4f}) deltas={body['metric_deltas']}"
    )
