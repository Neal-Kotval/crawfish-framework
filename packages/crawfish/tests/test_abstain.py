"""Abstention as a typed Output discipline (TS-4 / CRA-216).

Acceptance pinned here:

* a 0.5-confidence result under ``abstain_below(0.7)`` yields an :class:`Abstention`;
* an :class:`Abstention` type-checks as a valid Output value and is *routable*;
* a calibration-derived threshold differs from a naive constant on a mis-calibrated
  fixture (``abstain_below_calibrated``);
* ``calibrate``'s ``abstention_rate`` matches the share an ``abstain_below`` policy
  declines;
* an abstaining run still charges what it spent (abstention is post-hoc — it never
  refunds the cost of the run that produced the low-confidence answer);
* taint propagates into the Abstention (fail-safe: declining is always allowed).
"""

from __future__ import annotations

import pytest

from crawfish.abstain import (
    ABSTENTION_MARKER,
    Abstention,
    abstain_below,
    abstain_below_calibrated,
    is_abstention,
)
from crawfish.escalate import abstain_below as abstain_below_reexport
from crawfish.nodes.router import Classifier, Router
from crawfish.output import Output


# -- helpers ----------------------------------------------------------------
def _output(value: object, *, tainted: bool = False, lineage: str | None = None) -> Output:
    """A frozen Output carrying ``value`` (the unit an abstain discipline acts on)."""
    return Output(value=value, produced_by="node-1", tainted=tainted, lineage=lineage)


# -- core acceptance: low confidence -> Abstention --------------------------
def test_low_confidence_yields_abstention() -> None:
    out = _output({"answer": "maybe", "confidence": 0.5})
    result = abstain_below(0.7)(out)

    assert is_abstention(result.value)
    parsed = Abstention.from_value(result.value)
    assert parsed is not None
    assert parsed.confidence == 0.5
    assert parsed.threshold == 0.7


def test_confident_output_passes_through_unchanged() -> None:
    out = _output({"answer": "sure", "confidence": 0.95})
    result = abstain_below(0.7)(out)

    # Same Output instance returned: confident enough to act, no decline.
    assert result is out
    assert not is_abstention(result.value)


def test_confidence_at_threshold_acts() -> None:
    # >= threshold acts; the boundary is inclusive (mirrors abstention_threshold semantics).
    out = _output({"answer": "x", "confidence": 0.7})
    assert abstain_below(0.7)(out) is out


def test_missing_confidence_abstains_fail_safe() -> None:
    out = _output({"answer": "no confidence field here"})
    result = abstain_below(0.7)(out)

    assert is_abstention(result.value)
    parsed = Abstention.from_value(result.value)
    assert parsed is not None
    assert parsed.confidence is None  # nothing measured -> decline


def test_bare_numeric_value_is_measured() -> None:
    # extract_confidence falls back to a whole-value number when there's no field.
    assert is_abstention(abstain_below(0.7)(_output(0.4)).value)
    assert abstain_below(0.7)(_output(0.9)) is not None
    assert not is_abstention(abstain_below(0.7)(_output(0.9)).value)


# -- taint propagation ------------------------------------------------------
def test_taint_propagates_into_abstention() -> None:
    out = _output({"confidence": 0.2}, tainted=True, lineage="item-7")
    result = abstain_below(0.7)(out)

    assert result.tainted is True  # the carrying Output stays tainted
    assert result.lineage == "item-7"  # lineage threads through derive()
    parsed = Abstention.from_value(result.value, tainted=result.tainted)
    assert parsed is not None
    assert parsed.tainted is True


def test_untainted_stays_untainted() -> None:
    result = abstain_below(0.7)(_output({"confidence": 0.2}, tainted=False))
    assert result.tainted is False


# -- frozen / typed ---------------------------------------------------------
def test_abstention_is_frozen() -> None:
    from pydantic import ValidationError

    a = Abstention(reason="r", confidence=0.3, threshold=0.7)
    with pytest.raises(ValidationError):
        a.confidence = 0.9  # type: ignore[misc]


def test_as_value_round_trips() -> None:
    a = Abstention(reason="low", confidence=0.3, threshold=0.7, field="confidence")
    v = a.as_value()
    assert v[ABSTENTION_MARKER] is True
    back = Abstention.from_value(v)
    assert back is not None
    assert (back.reason, back.confidence, back.threshold, back.field) == (
        "low",
        0.3,
        0.7,
        "confidence",
    )


def test_from_value_rejects_non_abstention() -> None:
    assert Abstention.from_value({"answer": "x"}) is None
    assert Abstention.from_value("not a dict") is None
    assert Abstention.from_value(42) is None


def test_is_abstention_total_over_json() -> None:
    for v in (None, 1, "s", [], {}, {"_abstention": False}, {"_abstention": "yes"}):
        assert is_abstention(v) is False


# -- routability ------------------------------------------------------------
def test_abstention_is_routable() -> None:
    """A Router with an is_abstention predicate branches Abstention -> review."""
    classifier = Classifier.from_predicates(
        {"review": is_abstention},
        default="proceed",
    )
    review = _Sink("review")
    proceed = _Sink("proceed")
    router = Router({"review": review, "proceed": proceed}, classifier)

    declined = abstain_below(0.7)(_output({"confidence": 0.2}))
    confident = _output({"answer": "ok", "confidence": 0.99})

    assert router.route(declined)[0] == "review"
    assert router.route(confident)[0] == "proceed"


class _Sink:
    """A minimal branch target (a Router only needs a Node-shaped object to dispatch to)."""

    def __init__(self, name: str) -> None:
        self.name = name


# -- idempotence ------------------------------------------------------------
def test_abstain_is_idempotent() -> None:
    once = abstain_below(0.7)(_output({"confidence": 0.2}))
    twice = abstain_below(0.7)(once)
    assert twice is once  # already abstaining -> unchanged, not re-wrapped


# -- re-export identity -----------------------------------------------------
def test_escalate_reexports_abstain_below() -> None:
    assert abstain_below_reexport is abstain_below


# -- calibration-derived threshold (vs naive constant) ----------------------
def test_calibrated_threshold_differs_from_naive_on_miscalibration() -> None:
    """On a mis-calibrated report, the evidence-derived threshold beats a naive constant.

    A report whose reliability curve only clears target at high confidence yields a
    derived threshold well above a naively chosen 0.5 — so a 0.6-confidence answer that a
    naive ``abstain_below(0.5)`` would ACT on is correctly DECLINED by the calibrated
    policy.
    """
    from crawfish.metrics import CalibrationReport, ReliabilityBin
    from crawfish.runtime.base import DeterminismTier

    # Mis-calibrated: low/mid confidence bins are unreliable (accuracy < 0.9); only the
    # 0.95 bin clears target. abstention_threshold therefore derives ~0.95.
    report = CalibrationReport(
        org_id="local",
        definition_id="d",
        definition_version="1",
        content_sha="x",
        base_seed=0,
        runs=1,
        cases=3,
        determinism_tier=DeterminismTier.HONORS_SEED,
        reliability=(
            ReliabilityBin(confidence=0.6, accuracy=0.5, count=10),
            ReliabilityBin(confidence=0.8, accuracy=0.7, count=10),
            ReliabilityBin(confidence=0.95, accuracy=0.98, count=10),
        ),
        abstention_threshold=0.95,
    )

    out = _output({"answer": "x", "confidence": 0.6})
    naive = abstain_below(0.5)  # a guessed constant: would ACT on 0.6
    calibrated = abstain_below_calibrated(report)  # evidence-derived ~0.95: DECLINES

    assert not is_abstention(naive(out).value)
    assert is_abstention(calibrated(out).value)


# -- abstention_rate matches the policy's abstention share ------------------
async def test_abstention_rate_matches_policy_share(tmp_path) -> None:
    """``calibrate``'s ``abstention_rate`` equals what ``abstain_below`` would decline.

    Drive a tiny deterministic golden through calibrate, then apply
    ``abstain_below(report.abstention_threshold)`` to the same confidences and assert the
    declined share equals ``report.abstention_rate``.
    """
    import json

    from crawfish.core.context import RunContext
    from crawfish.definition.types import Definition
    from crawfish.eval import EvalCase
    from crawfish.metrics import calibrate
    from crawfish.runtime.base import AgentRuntime, DeterminismTier, RunRequest, RunResult

    # A definition with no declared outputs -> Output.value is the raw JSON text, which
    # extract_confidence and calibrate both read identically.
    definition = Definition(id="abstain-cal", name="abstain-cal")

    # Confidence keyed off the case id; the label is the category "yes". Some cases answer
    # "no" (wrong) so the reliability curve is non-trivial. ``confidence`` is the only
    # number in the emitted text, so extract_confidence reads it on the no-schema path.
    confidences = {"a": 0.2, "b": 0.4, "c": 0.6, "d": 0.8, "e": 0.95}
    answers = {"a": "no", "b": "no", "c": "yes", "d": "yes", "e": "yes"}

    class _ConfRuntime(AgentRuntime):
        name = "conf"
        determinism_tier = DeterminismTier.HONORS_SEED

        async def run(self, request: RunRequest, ctx: RunContext) -> RunResult:
            cid = request.inputs["case_id"]
            text = json.dumps({"category": answers[cid], "confidence": confidences[cid]})
            return RunResult(text=text, model="m", cost_usd=0.0)

    cases = [
        EvalCase(id=cid, inputs={"case_id": cid}, label={"category": "yes"}) for cid in confidences
    ]

    from crawfish.store import SqliteStore

    ctx = RunContext(store=SqliteStore(str(tmp_path / "c.db")), org_id="local")
    report = await calibrate(
        definition,
        cases,
        runs=1,
        ctx=ctx,
        runtime=_ConfRuntime(),
        inputs_for=lambda c: {"case_id": c.id},
    )

    # Apply the same calibration-derived threshold as a policy over the same outputs
    # (string-valued, exactly as calibrate measured them).
    discipline = abstain_below(report.abstention_threshold)
    declined = sum(
        1
        for cid, conf in confidences.items()
        if is_abstention(
            discipline(_output(json.dumps({"category": answers[cid], "confidence": conf}))).value
        )
    )
    assert declined / len(confidences) == pytest.approx(report.abstention_rate)


# -- an abstaining run still charges what it spent --------------------------
def test_abstention_does_not_refund_spend(tmp_path) -> None:
    """Abstention is post-hoc over a produced Output — it never refunds the run's cost."""
    from crawfish.core.context import CostBudget, RunContext
    from crawfish.store import SqliteStore

    ctx = RunContext(
        store=SqliteStore(str(tmp_path / "s.db")),
        cost_budget=CostBudget(limit_usd=1.0),
    )
    ctx.cost_budget.charge(0.25)  # the run that produced the low-confidence answer

    out = _output({"answer": "x", "confidence": 0.1})
    result = abstain_below(0.7)(out)

    assert is_abstention(result.value)
    # The discipline is pure over the Output — it touches no budget, so the spend stands.
    assert ctx.cost_budget.spent_usd == pytest.approx(0.25)
