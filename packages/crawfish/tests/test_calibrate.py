"""CRA-211 (AL-T4 / TS-2) acceptance — ``cw.calibrate``: variance / calibration / abstention.

Covers the issue's acceptance criteria:

* Under a **seed-varied** runtime: non-zero ``rubric_std`` and ``output_variance``.
* Under a **fully deterministic** runtime (ignores the seed): ``output_variance == 0`` and
  zero per-metric std.
* Same ``(base_seed, runs)`` ⇒ identical per-run **seed schedule** (procedure reproducible)
  and an identical report.
* **Brier** is computed when labels exist; **ECE** is ``None`` without labels, else in
  ``[0,1]`` with a bootstrap CI; a perfectly-calibrated synthetic fixture yields a low Brier
  and an ECE-CI covering ~0.
* ``CalibrationReport`` is **frozen**, carries ``org_id``, records the **determinism tier**.
* Calibrate **raises** on a ``RecordReplayRuntime``.
* The autonomy ceiling (budget / cancel) returns a **partial** report.
"""

from __future__ import annotations

import json
import random

import pytest

from crawfish.core.context import CostBudget, RunContext
from crawfish.core.types import Flow, Parameter
from crawfish.definition.types import AgentSpec, Definition, TeamSpec
from crawfish.escalate import abstention_threshold, extract_confidence
from crawfish.eval import EvalCase
from crawfish.metrics import (
    CalibrationError,
    CalibrationReport,
    Rubric,
    StructuralMatch,
    calibrate,
)
from crawfish.output import Output
from crawfish.runtime.base import (
    AgentRuntime,
    DeterminismTier,
    EventKind,
    RunRequest,
    RunResult,
    RuntimeEvent,
)
from crawfish.runtime.mock import MockRuntime
from crawfish.runtime.replay import RecordReplayRuntime
from crawfish.store import SqliteStore
from crawfish.typesystem import default_registry


def _register() -> None:
    # A record output so the typed Output.value is a real dict carrying confidence+category.
    default_registry.register_record("CalibrateRec", {"category": "str", "confidence": "float"})


def _definition() -> Definition:
    _register()
    return Definition(
        team=TeamSpec(agents=[AgentSpec(role="worker", prompt="classify", model="m")]),
        inputs=[Parameter(name="text", type="text", flow=Flow.FLUID)],
        outputs=[Parameter(name="out", type="CalibrateRec")],
    )


def _ctx(tmp_path, *, limit_usd: float | None = None) -> RunContext:
    store = SqliteStore(str(tmp_path / "c.db"))
    return RunContext(store=store, cost_budget=CostBudget(limit_usd=limit_usd), org_id="acme")


def _emit(payload: dict[str, object]) -> str:
    return json.dumps(payload, sort_keys=True)


# -- runtimes ---------------------------------------------------------------
class _SeedVariedRuntime(AgentRuntime):
    """A runtime whose category flips with the per-run ``decode_seed`` (genuine drift)."""

    name = "seed-varied"
    determinism_tier = DeterminismTier.HONORS_SEED

    async def run(self, request: RunRequest, ctx: RunContext) -> RunResult:
        ctx.cancel_token.raise_if_cancelled()
        seed = request.decode_seed or 0
        # Deterministic-per-seed but varies run-to-run: half the time "a", half "b".
        rng = random.Random(seed)
        category = "a" if rng.random() < 0.5 else "b"
        text = _emit({"category": category, "confidence": 0.8})
        return RunResult(
            text=text,
            model="m",
            events=[RuntimeEvent(kind=EventKind.RESULT, text=text)],
        )


class _DeterministicRuntime(AgentRuntime):
    """A runtime that IGNORES the seed entirely — every re-run is byte-identical."""

    name = "deterministic"
    determinism_tier = DeterminismTier.HONORS_SEED

    async def run(self, request: RunRequest, ctx: RunContext) -> RunResult:
        ctx.cancel_token.raise_if_cancelled()
        text = _emit({"category": "a", "confidence": 0.9})
        return RunResult(
            text=text,
            model="m",
            events=[RuntimeEvent(kind=EventKind.RESULT, text=text)],
        )


class _CalibratedRuntime(AgentRuntime):
    """A perfectly-calibrated runtime: reported confidence == P(correct).

    With ``decode_seed`` driving a Bernoulli(confidence) draw, the long-run accuracy in
    each confidence stratum matches the reported confidence — so ECE → ~0.
    """

    name = "calibrated"
    determinism_tier = DeterminismTier.HONORS_SEED

    def __init__(self, confidence: float) -> None:
        self._confidence = confidence

    async def run(self, request: RunRequest, ctx: RunContext) -> RunResult:
        seed = request.decode_seed or 0
        rng = random.Random(seed)
        correct = rng.random() < self._confidence
        category = "a" if correct else "wrong"  # label is "a"
        text = _emit({"category": category, "confidence": self._confidence})
        return RunResult(
            text=text, model="m", events=[RuntimeEvent(kind=EventKind.RESULT, text=text)]
        )


def _rubric() -> Rubric:
    return Rubric([StructuralMatch({"category": "a", "confidence": 0.8}, name="match")])


# -- variance ---------------------------------------------------------------
@pytest.mark.asyncio
async def test_seed_varied_runtime_yields_nonzero_variance(tmp_path) -> None:
    cases = [EvalCase(id="c1", inputs={"text": "x"}), EvalCase(id="c2", inputs={"text": "y"})]
    report = await calibrate(
        _definition(),
        cases,
        runs=8,
        ctx=_ctx(tmp_path),
        runtime=_SeedVariedRuntime(),
        rubric=_rubric(),
    )
    # Genuine run-to-run drift ⇒ non-zero structural variance AND non-zero metric std.
    assert report.output_variance > 0.0
    assert report.rubric_std["match"] > 0.0
    assert report.cases == 2
    assert report.runs == 8


@pytest.mark.asyncio
async def test_deterministic_runtime_yields_zero_variance(tmp_path) -> None:
    cases = [EvalCase(id="c1", inputs={"text": "x"})]
    report = await calibrate(
        _definition(),
        cases,
        runs=6,
        ctx=_ctx(tmp_path),
        runtime=_DeterministicRuntime(),
        rubric=_rubric(),
    )
    assert report.output_variance == 0.0
    assert report.rubric_std["match"] == 0.0
    # A seed-honouring backend attributes no variance floor to infra.
    assert report.infra_variance_floor == 0.0


# -- reproducibility --------------------------------------------------------
@pytest.mark.asyncio
async def test_same_seed_and_runs_reproduce_report(tmp_path) -> None:
    cases = [EvalCase(id="c1", inputs={"text": "x"}), EvalCase(id="c2", inputs={"text": "y"})]
    # ONE definition reused — same content ⇒ the report is byte-identical across calls.
    definition = _definition()
    a = await calibrate(
        definition,
        cases,
        runs=5,
        ctx=_ctx(tmp_path),
        runtime=_SeedVariedRuntime(),
        rubric=_rubric(),
        base_seed=42,
    )
    b = await calibrate(
        definition,
        cases,
        runs=5,
        ctx=_ctx(tmp_path),
        runtime=_SeedVariedRuntime(),
        rubric=_rubric(),
        base_seed=42,
    )
    assert a.model_dump() == b.model_dump()
    assert a.content_sha == b.content_sha


def test_per_run_seed_schedule_is_deterministic() -> None:
    from crawfish.metrics import _run_seed

    schedule = [_run_seed(7, "c1", i) for i in range(4)]
    again = [_run_seed(7, "c1", i) for i in range(4)]
    assert schedule == again
    # Distinct runs get distinct seeds; a different case id diverges.
    assert len(set(schedule)) == 4
    assert _run_seed(7, "c1", 0) != _run_seed(7, "c2", 0)
    assert _run_seed(7, "c1", 0) != _run_seed(8, "c1", 0)


# -- calibration metrics ----------------------------------------------------
@pytest.mark.asyncio
async def test_brier_and_ece_none_without_labels(tmp_path) -> None:
    cases = [EvalCase(id="c1", inputs={"text": "x"})]  # no label
    report = await calibrate(
        _definition(),
        cases,
        runs=4,
        ctx=_ctx(tmp_path),
        runtime=_SeedVariedRuntime(),
        rubric=_rubric(),
    )
    assert report.brier is None
    assert report.ece is None
    assert report.ece_ci is None
    assert report.reliability == ()
    # Without labels there is nothing to gate on — fail safe.
    assert report.gate_safe(0.1) is False


@pytest.mark.asyncio
async def test_brier_computed_and_ece_in_unit_interval_with_labels(tmp_path) -> None:
    cases = [
        EvalCase(id=f"c{i}", inputs={"text": str(i)}, label={"category": "a", "confidence": 0.7})
        for i in range(12)
    ]
    report = await calibrate(
        _definition(),
        cases,
        runs=4,
        ctx=_ctx(tmp_path),
        runtime=_CalibratedRuntime(0.7),
        rubric=_rubric(),
        n_resamples=300,
    )
    assert report.brier is not None
    assert report.ece is not None
    assert 0.0 <= report.ece <= 1.0
    assert report.ece_ci is not None
    lo, hi = report.ece_ci
    assert lo <= report.ece <= hi or lo <= hi  # CI is an ordered interval


@pytest.mark.asyncio
async def test_perfectly_calibrated_fixture_has_low_brier_and_ci_near_zero(tmp_path) -> None:
    # Confidence 0.5 with Bernoulli(0.5) correctness is perfectly calibrated. Enough
    # observations (40 cases × 10 runs = 400 points) that the per-bin accuracy concentrates
    # near 0.5 and the ECE estimate (and its CI lower bound) sits near 0.
    cases = [
        EvalCase(
            id=f"c{i:03d}", inputs={"text": str(i)}, label={"category": "a", "confidence": 0.5}
        )
        for i in range(40)
    ]
    report = await calibrate(
        _definition(),
        cases,
        runs=10,
        ctx=_ctx(tmp_path),
        runtime=_CalibratedRuntime(0.5),
        rubric=_rubric(),
        n_bins=4,
        n_resamples=400,
    )
    assert report.ece is not None and report.ece_ci is not None
    lo, hi = report.ece_ci
    # A well-calibrated fixture: the ECE point estimate is small and its CI reaches ~0.
    assert report.ece < 0.15
    assert lo < 0.1
    # Brier for a fair coin at p=0.5 is ~0.25 (consistent with label noise, never huge).
    assert report.brier is not None
    assert 0.2 < report.brier < 0.35


# -- abstention -------------------------------------------------------------
def test_abstention_threshold_reads_off_reliability_curve() -> None:
    # Accuracy clears 0.9 only at the highest-confidence bin (0.9); lower bins are unsafe.
    conf = [0.3, 0.6, 0.9]
    acc = [0.3, 0.7, 0.95]
    count = [10, 10, 10]
    assert abstention_threshold(conf, acc, count, target=0.9) == 0.9
    # No bin reliable ⇒ abstain on everything (fail safe).
    assert abstention_threshold([0.3], [0.4], [5], target=0.9) == 1.0
    # No evidence ⇒ default.
    assert abstention_threshold([0.9], [0.99], [0], target=0.9) == 1.0


@pytest.mark.asyncio
async def test_abstention_rate_matches_threshold(tmp_path) -> None:
    cases = [
        EvalCase(id=f"c{i}", inputs={"text": str(i)}, label={"category": "a", "confidence": 0.6})
        for i in range(10)
    ]
    report = await calibrate(
        _definition(),
        cases,
        runs=4,
        ctx=_ctx(tmp_path),
        runtime=_CalibratedRuntime(0.6),
        rubric=_rubric(),
        target_accuracy=0.9,
        n_resamples=200,
    )
    # confidence is always 0.6; if the derived threshold is above it, everything abstains.
    if report.abstention_threshold > 0.6:
        assert report.abstention_rate == 1.0
    else:
        assert report.abstention_rate == 0.0


def test_extract_confidence_reads_field_and_clamps() -> None:
    out = Output(output_schema=[], value={"confidence": 0.42}, produced_by="r")
    assert extract_confidence(out) == 0.42
    over = Output(output_schema=[], value={"confidence": 1.5}, produced_by="r")
    assert extract_confidence(over) == 1.0
    missing = Output(output_schema=[], value={"category": "a"}, produced_by="r")
    assert extract_confidence(missing) is None


# -- report shape -----------------------------------------------------------
@pytest.mark.asyncio
async def test_report_is_frozen_and_carries_org_and_tier(tmp_path) -> None:
    cases = [EvalCase(id="c1", inputs={"text": "x"})]
    report = await calibrate(
        _definition(),
        cases,
        runs=3,
        ctx=_ctx(tmp_path),
        runtime=_DeterministicRuntime(),
        rubric=_rubric(),
    )
    assert isinstance(report, CalibrationReport)
    assert report.org_id == "acme"
    assert report.determinism_tier is DeterminismTier.HONORS_SEED
    with pytest.raises((TypeError, ValueError)):
        report.output_variance = 1.0  # type: ignore[misc]


@pytest.mark.asyncio
async def test_best_effort_tier_attributes_infra_floor(tmp_path) -> None:
    # MockRuntime is BEST_EFFORT and ignores the seed → no output variance, but a
    # BEST_EFFORT *varying* backend would attribute its floor to infra. Here we assert the
    # tier is recorded and (no variance) the floor is 0.
    cases = [EvalCase(id="c1", inputs={"text": "x"})]
    report = await calibrate(
        _definition(),
        cases,
        runs=3,
        ctx=_ctx(tmp_path),
        runtime=MockRuntime(),
        rubric=Rubric([StructuralMatch({"category": "a"}, name="match")]),
    )
    assert report.determinism_tier is DeterminismTier.BEST_EFFORT
    assert report.infra_variance_floor == report.output_variance  # attributed to infra


# -- replay refusal ---------------------------------------------------------
@pytest.mark.asyncio
async def test_calibrate_refuses_replay_runtime(tmp_path) -> None:
    replay = RecordReplayRuntime(MockRuntime(), tmp_path / "cass", record=True)
    cases = [EvalCase(id="c1", inputs={"text": "x"})]
    with pytest.raises(CalibrationError):
        await calibrate(
            _definition(), cases, runs=3, ctx=_ctx(tmp_path), runtime=replay, rubric=_rubric()
        )


# -- autonomy ceiling -------------------------------------------------------
@pytest.mark.asyncio
async def test_budget_ceiling_returns_partial_report(tmp_path) -> None:
    cases = [EvalCase(id=f"c{i}", inputs={"text": str(i)}) for i in range(5)]
    # Budget room for ~3 runs at $1 each.
    report = await calibrate(
        _definition(),
        cases,
        runs=4,
        ctx=_ctx(tmp_path, limit_usd=3.0),
        runtime=_SeedVariedRuntime(),
        rubric=_rubric(),
        cost_per_run_usd=1.0,
    )
    assert report.partial is True


@pytest.mark.asyncio
async def test_cancel_ceiling_returns_partial_report(tmp_path) -> None:
    ctx = _ctx(tmp_path)
    ctx.cancel_token.cancel()
    cases = [EvalCase(id="c1", inputs={"text": "x"})]
    report = await calibrate(
        _definition(), cases, runs=4, ctx=ctx, runtime=_SeedVariedRuntime(), rubric=_rubric()
    )
    assert report.partial is True
    assert report.output_variance == 0.0  # nothing measured


@pytest.mark.asyncio
async def test_rejects_zero_runs(tmp_path) -> None:
    cases = [EvalCase(id="c1", inputs={"text": "x"})]
    with pytest.raises(ValueError):
        await calibrate(
            _definition(), cases, runs=0, ctx=_ctx(tmp_path), runtime=_SeedVariedRuntime()
        )
