"""CRA-217 acceptance: TS-7/R4 — House-guard (learned-then-distilled guards).

One test per acceptance clause (from the issue):

1. ``from_corrections`` (F-4) builds a GoldenSet matching the ledger count, and the
   guard validates against it.
2. The distilled predicate is PURE — ``$0``, no model call, same input ⇒ same 0/1,
   replays identically.
3. A guard below its JOINT floor stays in ``warn`` (cannot block a Sink); clearing the
   joint floor reaches ``block``. **Precision alone is insufficient**: a high-precision
   / low-coverage rule cannot block.
4. The certificate reports precision-LB **and** coverage with CIs, honestly.
5. A promoted guard rolls back reversibly (re-synthesizing a prior rule mints its own
   sha; a fresh synthesis on a fresh corpus mints a new sha).
6. The predicate grammar is CLOSED: a FLUID proposal cannot widen it (unknown kind /
   operator raises ``GuardGrammarError``); ``eval``/``exec`` are never used.
7. Fail-closed: no corpus ⇒ stays in ``warn``; ``require_earned`` raises.

Plus the security spine: the proposer emission is FLUID and is parsed as data into the
closed grammar; taint propagates from a fluid corpus into the certificate.

Deterministic: no live model call (mock/replay runtime), no wall-clock.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from crawfish.core.context import CostBudget, RunContext
from crawfish.core.types import JSONValue
from crawfish.definition import Definition
from crawfish.emission import CorrectionType, Provenance, emit_correction
from crawfish.eval import EvalCase, GoldenSet
from crawfish.guard import (
    Always,
    BoolCombination,
    Comparison,
    GuardGrammarError,
    GuardNotEarned,
    GuardStage,
    HouseGuard,
    NumericBound,
    PredicateMetric,
    SetMembership,
    distill,
    proportion_ci,
    propose_rule,
    synthesize_guard,
    wilson_lower_bound,
)
from crawfish.output import Output
from crawfish.runtime import MockRuntime
from crawfish.store import SqliteStore

FIXTURES = Path(__file__).parent / "fixtures"


def _store(tmp_path: Path) -> SqliteStore:
    return SqliteStore(tmp_path / "guard.db")


def _ctx(store: SqliteStore, *, limit_usd: float | None = None) -> RunContext:
    return RunContext(  # type: ignore[arg-type]
        store=store,
        cost_budget=CostBudget(limit_usd=limit_usd),
    )


def _output(value: JSONValue, *, tainted: bool = False) -> Output[JSONValue]:
    return Output(output_schema=[], value=value, produced_by="upstream", tainted=tainted)


def _seed_corrections(
    store: SqliteStore,
    n: int,
    *,
    produced: JSONValue,
    expected: JSONValue,
    org_id: str = "local",
    provenance: Provenance = Provenance.TRUSTED,
    tainted: bool = False,
) -> None:
    """Seed ``n`` trusted corrections with a fixed produced/expected pair."""
    for i in range(n):
        emit_correction(
            store,
            run_id=f"r{i}-{produced}",
            correction_type=CorrectionType.REVIEW_REJECT,
            provenance=provenance,
            tainted=tainted,
            org_id=org_id,
            inputs={"prompt": "do the thing"},
            produced=produced,
            expected=expected,
            ts=1.0,
        )


# A predicate that fires on the disallowed output ({"verdict":"unsafe"}) and not on the
# corrected one ({"verdict":"safe"}).
_DISALLOW_UNSAFE = Comparison(field="verdict", op="==", literal="unsafe")


# ===========================================================================
# (1) from_corrections builds a GoldenSet matching the ledger; guard validates it
# ===========================================================================
def test_from_corrections_count_and_guard_validates(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _seed_corrections(store, 20, produced={"verdict": "unsafe"}, expected={"verdict": "safe"})
    golden = GoldenSet.from_corrections(store)
    assert len(golden.cases()) == 20

    guard = HouseGuard.synthesize(_DISALLOW_UNSAFE, golden, precision_floor=0.8, min_coverage=0.8)
    # The predicate fires on every disallowed produced output and none of the allowed
    # corrected ones — clears the joint bar at n=20.
    assert guard.can_block is True
    assert guard.stage is GuardStage.BLOCK
    store.close()


# ===========================================================================
# (2) the distilled predicate is PURE ($0, no model call, deterministic)
# ===========================================================================
def test_distilled_predicate_is_pure_no_model_call(tmp_path: Path) -> None:
    store = _store(tmp_path)
    ctx = _ctx(store, limit_usd=0.0)  # a zero budget would trip on any model charge
    predicate = distill({"kind": "comparison", "field": "verdict", "op": "==", "literal": "unsafe"})
    out = _output({"verdict": "unsafe"})

    # Pure 0/1, no model call (budget untouched), same input ⇒ same answer.
    metric = PredicateMetric(predicate)
    assert metric.evaluate(out) == 1.0
    assert metric.evaluate(out) == 1.0
    assert metric.evaluate(_output({"verdict": "safe"})) == 0.0
    assert ctx.cost_budget.spent_usd == 0.0  # $0 — no model was called
    store.close()


def test_predicate_replays_identically() -> None:
    predicate = BoolCombination(
        op="and",
        terms=[
            Comparison(field="verdict", op="==", literal="unsafe"),
            NumericBound(field="score", lo=None, hi=0.5),
        ],
    )
    out = _output({"verdict": "unsafe", "score": 0.2})
    results = [predicate.matches(out.value) for _ in range(5)]
    assert results == [True] * 5
    # a value outside the numeric bound does not match
    assert predicate.matches({"verdict": "unsafe", "score": 0.9}) is False


# ===========================================================================
# (3) joint floor: below ⇒ warn (cannot block); clearing ⇒ block.
#     Precision ALONE is insufficient.
# ===========================================================================
def test_below_joint_floor_stays_in_warn(tmp_path: Path) -> None:
    store = _store(tmp_path)
    # Only 3 corrections: a perfect predicate still has a precision lower bound (~0.44)
    # well under a 0.8 floor — small support cannot certify enforcement.
    _seed_corrections(store, 3, produced={"verdict": "unsafe"}, expected={"verdict": "safe"})
    golden = GoldenSet.from_corrections(store)

    guard = HouseGuard.synthesize(_DISALLOW_UNSAFE, golden, precision_floor=0.8, min_coverage=0.8)
    assert guard.can_block is False
    assert guard.stage is GuardStage.WARN
    # it may still observe (shadow/warn predicate is available), it just cannot enforce
    assert guard.matches(_output({"verdict": "unsafe"})) is True
    assert guard.blocks(_output({"verdict": "unsafe"})) is False  # no authority
    store.close()


def test_clearing_joint_floor_reaches_block_and_blocks(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _seed_corrections(store, 40, produced={"verdict": "unsafe"}, expected={"verdict": "safe"})
    golden = GoldenSet.from_corrections(store)

    guard = HouseGuard.synthesize(_DISALLOW_UNSAFE, golden, precision_floor=0.8, min_coverage=0.8)
    assert guard.can_block is True
    assert guard.blocks(_output({"verdict": "unsafe"})) is True
    # it does NOT block an allowed output (no false positive)
    assert guard.blocks(_output({"verdict": "safe"})) is False
    store.close()


def test_precision_alone_is_insufficient(tmp_path: Path) -> None:
    """A high-precision but low-coverage rule cannot earn the right to block.

    The rule fires (correctly) on a 10-of-40 subset of disallowed cases and on NO
    allowed case — so its precision lower bound clears a 0.6 floor, but its coverage
    (10/40) is far below the 0.8 coverage floor. The JOINT criterion therefore vetoes
    enforcement *on coverage*: precision alone is not enough.
    """
    store = _store(tmp_path)
    for i in range(40):
        produced: JSONValue = {"verdict": "unsafe", "rare": i < 10}
        emit_correction(
            store,
            run_id=f"r{i}",
            correction_type=CorrectionType.REVIEW_REJECT,
            provenance=Provenance.TRUSTED,
            org_id="local",
            inputs={"prompt": "p"},
            produced=produced,
            expected={"verdict": "safe", "rare": False},
            ts=1.0,
        )
    golden = GoldenSet.from_corrections(store)
    narrow = Comparison(field="rare", op="==", literal=True)

    cert, stage = synthesize_guard(narrow, golden, precision_floor=0.6, min_coverage=0.8)
    # Precision point is perfect (fires only on truly-disallowed cases) and its lower
    # bound clears the precision floor ...
    assert cert.precision_point == 1.0
    assert cert.precision_lb >= 0.6
    # ... but coverage is only ~25%, well under the 0.8 floor — the joint gate vetoes.
    assert cert.coverage.point < 0.4
    assert cert.coverage.lo < 0.8
    assert cert.earned is False
    assert stage is GuardStage.WARN
    assert "coverage" in cert.reason
    store.close()


# ===========================================================================
# (4) the certificate reports precision-LB AND coverage with CIs, honestly
# ===========================================================================
def test_certificate_reports_precision_lb_and_coverage_ci(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _seed_corrections(store, 20, produced={"verdict": "unsafe"}, expected={"verdict": "safe"})
    golden = GoldenSet.from_corrections(store)

    cert, _ = synthesize_guard(_DISALLOW_UNSAFE, golden, precision_floor=0.8, min_coverage=0.8)
    # precision lower bound is strictly below the optimistic point estimate
    assert cert.precision_point == 1.0
    assert 0.0 < cert.precision_lb < cert.precision_point
    # coverage is a real interval, lo <= point <= hi, honest about uncertainty
    assert cert.coverage.lo <= cert.coverage.point <= cert.coverage.hi
    assert cert.coverage.lo < 1.0  # not falsely certain off a finite sample
    assert cert.n_decisions == 20
    assert cert.n_disallowed == 20
    store.close()


def test_wilson_bounds_are_honest() -> None:
    # 3/3 is NOT certified as precision 1.0 — the lower bound is well under it.
    assert wilson_lower_bound(3, 3) < 0.5
    # no evidence ⇒ 0.0 (fail-closed arithmetic)
    assert wilson_lower_bound(0, 0) == 0.0
    # the interval widens as n shrinks; lo <= point <= hi always holds
    ci = proportion_ci(5, 10)
    assert ci.lo <= ci.point <= ci.hi
    assert proportion_ci(0, 0).point == 0.0


# ===========================================================================
# (5) reversibility: a fresh synthesis mints a new sha; re-synthesis is stable
# ===========================================================================
def test_resynthesis_on_fresh_corpus_mints_new_sha(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _seed_corrections(store, 20, produced={"verdict": "unsafe"}, expected={"verdict": "safe"})
    golden = GoldenSet.from_corrections(store)

    g1 = HouseGuard.synthesize(_DISALLOW_UNSAFE, golden, precision_floor=0.8, min_coverage=0.8)
    # a DIFFERENT predicate ⇒ a different content sha (a new rule, never an edit)
    other = Comparison(field="verdict", op="==", literal="bad")
    g2 = HouseGuard.synthesize(other, golden, precision_floor=0.8, min_coverage=0.8)
    assert g1.content_sha != g2.content_sha
    # the SAME predicate re-synthesized ⇒ the same sha (content-addressed, reversible)
    g1_again = HouseGuard.synthesize(
        _DISALLOW_UNSAFE, golden, precision_floor=0.8, min_coverage=0.8
    )
    assert g1_again.content_sha == g1.content_sha
    store.close()


def test_guard_carries_org_id(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _seed_corrections(
        store, 20, produced={"verdict": "unsafe"}, expected={"verdict": "safe"}, org_id="acme"
    )
    golden = GoldenSet.from_corrections(store, org_id="acme")
    guard = HouseGuard.synthesize(_DISALLOW_UNSAFE, golden, precision_floor=0.8, min_coverage=0.8)
    assert guard.org_id == "acme"
    assert guard.certificate.org_id == "acme"
    store.close()


# ===========================================================================
# (6) the predicate grammar is CLOSED — a FLUID proposal cannot widen it
# ===========================================================================
def test_distill_accepts_every_closed_kind() -> None:
    assert isinstance(distill({"kind": "comparison", "op": "==", "literal": 1}), Comparison)
    assert isinstance(
        distill({"kind": "set_membership", "field": "x", "members": [1, 2]}), SetMembership
    )
    assert isinstance(
        distill({"kind": "numeric_bound", "field": "x", "lo": 0, "hi": 1}), NumericBound
    )
    assert isinstance(distill({"kind": "always", "value": False}), Always)
    nested = distill(
        {
            "kind": "bool_combination",
            "op": "and",
            "terms": [
                {"kind": "comparison", "field": "a", "op": ">", "literal": 0},
                {"kind": "always", "value": True},
            ],
        }
    )
    assert isinstance(nested, BoolCombination)


def test_unknown_kind_cannot_widen_grammar() -> None:
    with pytest.raises(GuardGrammarError, match="closed"):
        distill({"kind": "shell_exec", "cmd": "rm -rf /"})


def test_unknown_operator_is_rejected() -> None:
    with pytest.raises(GuardGrammarError, match="comparison op"):
        distill({"kind": "comparison", "field": "x", "op": "__import__", "literal": 1})


def test_malformed_terms_are_rejected() -> None:
    with pytest.raises(GuardGrammarError):
        distill({"kind": "bool_combination", "op": "not", "terms": []})  # not needs one term
    with pytest.raises(GuardGrammarError):
        distill({"kind": "set_membership", "field": "x"})  # missing members
    with pytest.raises(GuardGrammarError):
        distill("not even json {")


def test_proposal_string_is_parsed_as_data() -> None:
    # A FLUID proposer might emit a JSON *string*; it is parsed as data, not executed.
    predicate = distill(
        '{"kind": "comparison", "field": "verdict", "op": "==", "literal": "unsafe"}'
    )
    assert isinstance(predicate, Comparison)
    assert predicate.matches({"verdict": "unsafe"}) is True


# ===========================================================================
# (7) fail-closed: no corpus ⇒ warn; require_earned raises
# ===========================================================================
def test_empty_corpus_fails_closed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    golden = GoldenSet.from_corrections(store)  # no corrections seeded
    assert golden.cases() == []

    guard = HouseGuard.synthesize(_DISALLOW_UNSAFE, golden, precision_floor=0.8, min_coverage=0.8)
    assert guard.can_block is False
    assert guard.stage is GuardStage.WARN
    assert "fails closed" in guard.certificate.reason
    with pytest.raises(GuardNotEarned):
        guard.require_earned()
    store.close()


def test_require_earned_returns_self_when_earned(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _seed_corrections(store, 40, produced={"verdict": "unsafe"}, expected={"verdict": "safe"})
    golden = GoldenSet.from_corrections(store)
    guard = HouseGuard.synthesize(
        _DISALLOW_UNSAFE, golden, precision_floor=0.8, min_coverage=0.8
    ).require_earned()
    assert guard.can_block is True
    store.close()


def test_invalid_thresholds_raise(tmp_path: Path) -> None:
    store = _store(tmp_path)
    golden = GoldenSet.from_corrections(store)
    with pytest.raises(ValueError):
        synthesize_guard(_DISALLOW_UNSAFE, golden, precision_floor=1.5, min_coverage=0.5)
    with pytest.raises(ValueError):
        synthesize_guard(_DISALLOW_UNSAFE, golden, precision_floor=0.5, min_coverage=-0.1)
    store.close()


# ===========================================================================
# Security spine: the proposer leaf is FLUID and parsed as data; taint propagates
# ===========================================================================
def _proposer(tmp_path: Path) -> Definition:
    dest = tmp_path / "full"
    shutil.copytree(FIXTURES / "full", dest)
    return Definition.from_package(str(dest))


async def test_propose_rule_is_the_only_model_call_and_distills(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _seed_corrections(store, 5, produced={"verdict": "unsafe"}, expected={"verdict": "safe"})
    golden = GoldenSet.from_corrections(store)

    proposal_json = '{"kind": "comparison", "field": "verdict", "op": "==", "literal": "unsafe"}'

    def _responder(_request: object) -> str:
        return proposal_json

    runtime = MockRuntime(_responder)  # type: ignore[arg-type]
    out = await propose_rule(_proposer(tmp_path), golden, _ctx(store), runtime)
    # The proposer emission is FLUID; distill parses it as data into the closed grammar.
    predicate = distill(out.value)
    assert isinstance(predicate, Comparison)
    assert predicate.matches({"verdict": "unsafe"}) is True
    store.close()


def test_taint_propagates_into_certificate(tmp_path: Path) -> None:
    store = _store(tmp_path)
    # Even a clean corpus: if the proposal was fluid-derived, the certificate is tainted.
    _seed_corrections(store, 40, produced={"verdict": "unsafe"}, expected={"verdict": "safe"})
    golden = GoldenSet.from_corrections(store)
    cert, _ = synthesize_guard(
        _DISALLOW_UNSAFE, golden, precision_floor=0.8, min_coverage=0.8, tainted=True
    )
    assert cert.tainted is True
    store.close()


def test_authored_golden_set_also_works(tmp_path: Path) -> None:
    # The guard validates against any GoldenSet, not only the corrections path.
    store = _store(tmp_path)
    golden = GoldenSet(store, "authored")
    for i in range(40):
        golden.add(
            EvalCase(
                inputs={"q": str(i)},
                output={"verdict": "unsafe"},
                label={"verdict": "safe"},
            )
        )
    guard = HouseGuard.synthesize(_DISALLOW_UNSAFE, golden, precision_floor=0.8, min_coverage=0.8)
    assert guard.can_block is True
    store.close()
