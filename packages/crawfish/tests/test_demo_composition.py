"""Deterministic acceptance test for the Milestone-2 composition step of the cumulative demo.

Exercises the composition surface (CRA-205..208) added to
``demo/triage-bot/self_improve.py`` — entirely off the mock runtime (NO live model
call) — and asserts the three load-bearing M2 guarantees:

* **Router branch (C1)** — a runnable :class:`Router` (``branch()``-style) routes each
  ticket by its (fluid) type down ONE STATIC branch; more than one branch fires, and the
  fluid label only *selects* among the closed, pre-declared branch set (never synthesises
  a new target — the security-spine invariant).
* **Bounded recurse (C3)** — a multi-part ticket is split and handled by a depth-guarded
  :func:`recurse`: it stops on its pure ``base_case`` *within* the static ``max_depth``
  bound (never wall-clock), folding its descent-order sub-answers into one reply.
* **Durable back-edge resume (C2b)** — a re-run of the SAME recurse with ``resume=True``
  over the same F-2 ledger replays every committed level at **$0** (proven as a dollar
  delta) and reproduces the folded reply bit-for-bit (content-sha verified).

Plus the assembly-safety invariants the changelog promises: an unbounded recurse is
rejected at construction (``UnboundedRecursionError``), and a Router with an uncovered
classifier label is rejected at construction (``UnroutableLabelError``).
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest

from crawfish.core.context import CostBudget, RunContext
from crawfish.ledger import ExecutionLedger
from crawfish.nodes import UnroutableLabelError
from crawfish.output import Output, output_content_sha
from crawfish.store import SqliteStore
from crawfish.workflow import UnboundedRecursionError, recurse

REPO_ROOT = Path(__file__).resolve().parents[3]
SCENARIO = REPO_ROOT / "demo" / "triage-bot" / "self_improve.py"


def _load_scenario():
    spec = importlib.util.spec_from_file_location("crawfish_demo_composition_test", SCENARIO)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # so dataclass forward-refs resolve
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def module():
    if not SCENARIO.exists():  # pragma: no cover - demo always present in-repo
        pytest.skip(f"demo scenario not found at {SCENARIO}")
    return _load_scenario()


@pytest.fixture(scope="module")
def result(module):
    return module.run_self_improvement(live=False)  # deterministic mock path only


# --- Router branch: routes by fluid type, hits more than one static branch -------
def test_router_routed_every_ticket(module, result) -> None:
    """Every seed ticket was classified and dispatched down a branch (none dropped)."""
    assert sum(result.router_routed.values()) == len(module._SEED_TICKETS)


def test_router_hit_multiple_branches(result) -> None:
    """The Router actually *branches* — more than one arm fired (not a passthrough).

    The seed tickets span bug / billing / feature, so a correct type-router lands them on
    at least those three distinct branches.
    """
    assert result.router_branches_hit > 1
    assert {"bug", "billing", "feature"} <= set(result.router_routed)


def test_router_label_is_a_closed_static_set(module) -> None:
    """A fluid label can only SELECT among the pre-declared (static) branches.

    The classifier's label set is closed at construction; a label outside it cannot be
    synthesised. Constructing a Router whose branches miss a classifier label is rejected
    at assembly (``UnroutableLabelError``) — the routing graph is total before it runs.
    """
    router = module._build_router()
    assert set(router.branches) == set(module._ROUTER_LABELS)
    # every routed label is one of the static branches (never a fresh, fluid-derived target)
    full = module.run_self_improvement(live=False)
    assert set(full.router_routed) <= set(module._ROUTER_LABELS)


def test_uncovered_router_label_is_rejected_at_construction(module) -> None:
    """A classifier label with no matching branch fails at assembly (totality check)."""
    from crawfish.nodes import Classifier, Router

    classifier = Classifier.from_predicates({"bug": lambda _v: True}, default="how-to", name="t")
    with pytest.raises(UnroutableLabelError):
        # missing the 'how-to' default branch -> unroutable
        Router({"bug": module._build_router().branches["bug"]}, classifier)


# --- Bounded recurse: base-case stop within the static depth bound, folds --------
def test_recurse_stopped_on_base_case_within_bound(module, result) -> None:
    """The recurse stopped on its pure base case, strictly within the static depth bound.

    A multi-part ticket with N parts descends N levels and stops on ``base_case`` (all
    parts answered), never reaching ``max_depth`` — proving the base case (not the bound)
    is what halts a healthy run, and the bound is never exceeded.
    """
    assert result.recurse_stopped == "base_case"
    assert 0 < result.recurse_depth_reached <= result.recurse_max_depth
    assert result.recurse_depth_reached == module._MULTI_PART_COUNT


def test_recurse_folded_all_parts(module, result) -> None:
    """The descent-order sub-answers were folded into ONE reply (the combine reducer)."""
    assert result.recurse_parts_folded == module._MULTI_PART_COUNT
    assert result.recurse_final_sha


def test_recurse_never_exceeds_max_depth_even_unsatisfiable(module) -> None:
    """With a never-true base case the recurse is STILL bounded — it stops at max_depth.

    This proves the depth bound is load-bearing independently of the base case: the
    descent runs exactly ``max_depth`` levels and no more (never wall-clock, never
    unbounded).
    """
    store = SqliteStore()
    ctx = RunContext(store=store, org_id="acme", cost_budget=CostBudget(limit_usd=3.0))
    backend = module._make_backend(live=False, record=False, model=None)
    body = module._build_recurse_body()
    rec = recurse(
        body,
        base_case=lambda _out: False,  # never satisfied
        max_depth=module.RECURSE_MAX_DEPTH,
        combine=module._fold_sub_answers,
        edge_id=module.RECURSE_EDGE_ID,
        name="multipart-recurse",
    )
    seed = Output(
        value={"ticket_body": module._MULTI_PART_TICKET, "_recurse_depth": 0},
        produced_by="recurse-seed",
        lineage=module._MULTI_PART_TICKET,
        output_schema=[],
    )
    res = asyncio.run(rec.execute(seed, ctx, backend.runtime))
    assert res.stopped == "max_depth"
    assert res.depth_reached == module.RECURSE_MAX_DEPTH
    store.close()


def test_unbounded_recurse_is_rejected_at_construction(module) -> None:
    """An unbounded recursion (``max_depth=None``) is rejected at assembly."""
    body = module._build_recurse_body()
    with pytest.raises(UnboundedRecursionError):
        recurse(
            body,
            base_case=lambda _out: True,
            max_depth=None,
            combine=module._fold_sub_answers,
        )


# --- Durable back-edge resume: re-run re-charges $0, bit-identical ---------------
def test_recurse_resume_is_zero_dollars(result) -> None:
    """A resume over the same ledger replays every committed level at $0 (C2b)."""
    assert result.recurse_resume_spent_usd == 0.0


def test_recurse_checkpoints_each_level_and_resume_is_bit_identical(module) -> None:
    """Every descent level checkpoints into the org-scoped ledger; resume reproduces it.

    The first run commits each level to the F-2 depth-variant ledger; a second run with
    ``resume=True`` over the SAME ledger replays them and folds a bit-identical reply
    (content-sha verified). A different org sees none of the committed levels (tenancy).
    """
    store = SqliteStore()
    ctx = RunContext(store=store, org_id="acme", cost_budget=CostBudget(limit_usd=3.0))
    backend = module._make_backend(live=False, record=False, model=None)
    rec = module._build_recurse(module._MULTI_PART_COUNT)
    ledger = ExecutionLedger(store, org_id="acme")
    seed = Output(
        value={"ticket_body": module._MULTI_PART_TICKET, "_recurse_depth": 0},
        produced_by="recurse-seed",
        lineage=module._MULTI_PART_TICKET,
        output_schema=[],
    )
    first = asyncio.run(rec.execute(seed, ctx, backend.runtime, ledger=ledger, resume=False))
    loop_id = rec._loop_id(module._MULTI_PART_TICKET)
    completed = ledger.completed_depths(loop_id, module._MULTI_PART_TICKET)
    assert completed == set(range(first.depth_reached))
    # a different org's ledger sees none of it (tenancy)
    other = ExecutionLedger(store, org_id="other-org")
    assert other.completed_depths(loop_id, module._MULTI_PART_TICKET) == set()
    # the resume of the SAME recursion reproduces the folded reply bit-for-bit
    resumed = asyncio.run(rec.execute(seed, ctx, backend.runtime, ledger=ledger, resume=True))
    assert output_content_sha(resumed.output) == output_content_sha(first.output)
    store.close()


# --- the whole-scenario PASS predicate now gates on the M2 step ------------------
def test_scenario_pass_predicate_requires_composition(result) -> None:
    """The cumulative PASS predicate gates on the Router + recurse evidence."""
    assert result.passed(), result.summary()


def test_composition_deterministic_across_runs(module) -> None:
    """The router routing and the folded recurse sha are bit-identical across runs."""
    a = module.run_self_improvement(live=False)
    b = module.run_self_improvement(live=False)
    assert a.router_routed == b.router_routed
    assert a.recurse_final_sha == b.recurse_final_sha
    assert a.recurse_stopped == b.recurse_stopped == "base_case"
