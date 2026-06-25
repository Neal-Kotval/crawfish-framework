"""UNFILED-OPTIMIZE — ``craw code optimize`` orchestrates tune/refine/learn, proposing only.

Pins the M4.5 optimization plane's load-bearing invariants:

* it **proposes a winner, never auto-promotes** (``promoted`` is always ``False``; the active
  version pointer is M6's human gate's job);
* it **seeds a regression baseline** before driving the inner loop (the F-3 promotion gate
  needs one);
* it **honours ``--budget``** — an exhausted ceiling halts with ``stopped_reason="budget"``;
* it is **deterministic under ``--seed``** (same seed ⇒ identical winner + trial order);
* it **fires no Sink** (eval-mode / frozen) and makes **no live model call** (a deterministic
  ``MockRuntime`` drives the search);
* the ``--json`` body is the versioned ``craw.code.optimize.v1`` envelope.

All data is pre-seeded; the runtime + benchmark are injected so nothing touches a backend.
"""

from __future__ import annotations

import json as _json
from pathlib import Path

import pytest

from crawfish.batch import Task
from crawfish.code.optimize import (
    NoBaselineError,
    optimize_component,
    scaffold_tune_toml,
    select_mode,
)
from crawfish.definition.types import AgentSpec, Definition, TeamSpec
from crawfish.eval import load_baseline
from crawfish.metrics import Benchmark, OutputNumber, Rubric
from crawfish.runtime.base import RunRequest
from crawfish.runtime.mock import MockRuntime
from crawfish.runtime.prompt import pick_agent
from crawfish.store import SqliteStore

# A component package with a searchable model knob space (a model LIST → tune mode).
_INSTRUCTIONS = """---
role: main
model:
  - claude-haiku-4-5
  - claude-sonnet-4-6
  - claude-opus-4-8
---
Score the task.
"""

_DEFINITION_PY = """from __future__ import annotations

from crawfish.core import Flow, Parameter

inputs = [Parameter(name="task", type="str", flow=Flow.FLUID)]
outputs = [Parameter(name="score", type="str")]
"""


def _component(tmp_path: Path) -> str:
    """Write a minimal on-disk component with a tunable model list. Returns its path."""
    comp = tmp_path / "definitions" / "triage"
    comp.mkdir(parents=True)
    (comp / "instructions.md").write_text(_INSTRUCTIONS)
    (comp / "definition.py").write_text(_DEFINITION_PY)
    return str(comp)


def _store(tmp_path: Path) -> SqliteStore:
    return SqliteStore(tmp_path / "opt.db")


def _ladder_runtime() -> MockRuntime:
    """A deterministic responder: a better model ⇒ a higher score (no live call, $0)."""
    ladder = {"claude-haiku-4-5": 7, "claude-sonnet-4-6": 8, "claude-opus-4-8": 9}

    def _responder(request: RunRequest) -> str:
        agent = pick_agent(request.definition, request.role)
        model = agent.model[0] if isinstance(agent.model, list) else agent.model
        return _json.dumps({"score": ladder.get(model or "", 5)})

    return MockRuntime(_responder)


def _benchmark() -> Benchmark:
    rubric = Rubric([OutputNumber(field="score", name="score")])
    return Benchmark(rubric, [Task(description="a"), Task(description="b")])


def _run(tmp_path: Path, **kw: object) -> dict[str, object]:
    store = _store(tmp_path)
    try:
        return optimize_component(
            _component(tmp_path),
            store=store,
            runtime=_ladder_runtime(),
            benchmark=_benchmark(),
            **kw,  # type: ignore[arg-type]
        )
    finally:
        store.close()


# -- mode selection ----------------------------------------------------------
def test_select_mode_picks_tune_for_a_model_grid() -> None:
    """A model LIST is a searchable knob space → auto picks tune."""
    d = Definition(
        team=TeamSpec(agents=[AgentSpec(role="main", model=["a", "b", "c"])]),
    )
    assert select_mode(d) == "tune"


def test_select_mode_falls_back_to_refine_without_a_knob_space() -> None:
    """A single pinned model has no grid to search → refine toward a Rubric bound."""
    d = Definition(team=TeamSpec(agents=[AgentSpec(role="main", model="only-one")]))
    assert select_mode(d) == "refine"


# -- the pass: proposes, never promotes -------------------------------------
def test_optimize_proposes_a_winner_and_never_promotes(tmp_path: Path) -> None:
    """The pass returns a winner + per-metric deltas but NEVER flips the active version."""
    body = _run(tmp_path, mode="auto", seed=7)
    assert body["mode"] == "tune"  # the model grid drove tune
    # THE load-bearing invariant: optimize proposes; it never auto-promotes (M6 gate does).
    assert body["promoted"] is False
    assert body["winner_sha"]  # a concrete winner sha is proposed
    assert "metric_deltas" in body and isinstance(body["metric_deltas"], dict)
    assert body["stopped_reason"] in ("exhausted", "max_trials")
    assert body["component"].endswith("definitions/triage")


def test_optimize_seeds_a_regression_baseline(tmp_path: Path) -> None:
    """A baseline is seeded before the loop so the promotion gate has a bar to compare to."""
    store = _store(tmp_path)
    try:
        optimize_component(
            _component(tmp_path),
            store=store,
            runtime=_ladder_runtime(),
            benchmark=_benchmark(),
            seed=7,
        )
        assert load_baseline(store, "optimize:triage", org_id="local") is not None
    finally:
        store.close()


def test_optimize_scaffolds_tune_toml_only_when_absent(tmp_path: Path) -> None:
    """A tune.toml is scaffolded once (reference-only) and never clobbered on a second pass."""
    comp = _component(tmp_path)
    assert scaffold_tune_toml(comp) is True
    body_text = (Path(comp) / "tune.toml").read_text()
    # Reference-only: a knob domain, no inline secret/destination value assignment (CRA-276).
    assert "[[knob]]" in body_text
    for forbidden in ("sk-", "api_key", "password =", "token =", "secret ="):
        assert forbidden not in body_text.lower()
    # Second call never clobbers an authored/scaffolded tune.toml.
    assert scaffold_tune_toml(comp) is False


# -- budget honoured ---------------------------------------------------------
def test_over_budget_halts_with_budget_stopped_reason(tmp_path: Path) -> None:
    """A zero ceiling halts the search before any trial: stopped_reason='budget', no promote."""
    body = _run(tmp_path, mode="tune", seed=7, budget_usd=0.0)
    assert body["stopped_reason"] == "budget"
    assert body["promoted"] is False


# -- determinism -------------------------------------------------------------
def test_same_seed_proposes_the_same_winner(tmp_path: Path) -> None:
    """A fixed --seed ⇒ identical winner sha + stopped_reason (no stochastic leaf)."""
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    a = _run(tmp_path / "a", mode="tune", seed=11)
    b = _run(tmp_path / "b", mode="tune", seed=11)
    assert a["winner_sha"] == b["winner_sha"]
    assert a["stopped_reason"] == b["stopped_reason"]
    assert a["metric_deltas"] == b["metric_deltas"]


# -- the --json envelope -----------------------------------------------------
def test_json_envelope_is_versioned_optimize_v1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--json`` emits the versioned craw.code.optimize.v1 envelope with the proposal body."""
    from crawfish.code import emit_json

    body = _run(tmp_path, mode="tune", seed=7)
    emit_json("code.optimize", body, org="local")
    out = _json.loads(capsys.readouterr().out)
    assert out["schema"] == "craw.code.optimize.v1"
    assert out["org"] == "local"
    assert out["promoted"] is False  # the proposal-not-promotion invariant survives the wire
    assert out["winner_sha"] == body["winner_sha"]


# -- no baseline -> exit-5 signal -------------------------------------------
def test_no_baseline_raises(tmp_path: Path) -> None:
    """An empty benchmark (no scores) means no baseline could be seeded (the exit-5 signal)."""
    store = _store(tmp_path)
    # A rubric with no metrics scores to an empty vector → no baseline can be seeded.
    empty = Benchmark(Rubric([]), [Task(description="a")])
    try:
        with pytest.raises(NoBaselineError):
            optimize_component(
                _component(tmp_path),
                store=store,
                runtime=_ladder_runtime(),
                benchmark=empty,
                seed=7,
            )
    finally:
        store.close()
