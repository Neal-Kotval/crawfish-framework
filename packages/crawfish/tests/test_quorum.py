"""CRA-215 / TS-1 acceptance: typed quorum / self-consistency aggregator.

A :class:`QuorumRuntime` wraps any inner runtime, samples the *same* request k times
(each charging the shared budget, each a distinct seeded leaf), and reduces the k results
by a typed, pure consensus vote. These tests pin the acceptance criteria:

* k=5 over a ``RecordReplayRuntime`` returns the majority typed Output, with k DISTINCT
  cassette keys (via the F-1 ``sample_index`` coordinate);
* a budget below k× per-call stops early and never exceeds the ceiling;
* the sequential proportion test stops once the lead is statistically real;
* same base seed ⇒ identical sample count + winner;
* ``majority_vote(field="label")`` collapses ``{"a":1,"b":2}`` / ``{"b":2,"a":1}``;
* a high-cardinality (all-distinct) vote abstains;
* cancel between samples is honoured;
* the winner is tainted iff any sample was tainted (taint union, ALG-7).

All deterministic — no live model calls (mock / record-replay only).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from crawfish.core.context import BudgetExceeded, CostBudget, RunContext
from crawfish.core.types import Flow, Parameter
from crawfish.definition.types import AgentSpec, Coordination, Definition, TeamSpec
from crawfish.runtime.base import AgentRuntime, RunRequest, RunResult
from crawfish.runtime.quorum import (
    MajorityVote,
    QuorumAbstention,
    QuorumRuntime,
    majority_vote,
    quorum_output,
)
from crawfish.runtime.replay import RecordReplayRuntime
from crawfish.store import SqliteStore

# --------------------------------------------------------------------------- #
# Fixtures: a Definition (with a fluid input) + controllable inner runtimes.   #
# --------------------------------------------------------------------------- #


def _definition(*, fluid: bool = False, sample_k: int | None = None) -> Definition:
    inputs = [Parameter(name="claim", type="str", flow=Flow.FLUID)] if fluid else []
    return Definition(
        id="quorum-test",
        inputs=inputs,
        team=TeamSpec(
            agents=[AgentSpec(role="a", prompt="x", sample_k=sample_k)],
            coordination=Coordination.SINGLE,
        ),
    )


def _request(*, fluid: bool = False, sample_k: int | None = None) -> RunRequest:
    d = _definition(fluid=fluid, sample_k=sample_k)
    inputs = {"claim": "untrusted"} if fluid else {}
    return RunRequest(definition=d, role="a", inputs=inputs)


class _SeededRuntime(AgentRuntime):
    """Returns a result drawn from a per-seed table, so samples vary deterministically.

    ``votes`` maps the derived per-sample ``decode_seed`` is impractical to predict, so
    instead the runtime cycles a provided list keyed by *call order* — the quorum draws
    exactly k times, in order, so call N returns ``texts[N]``. Cost is fixed per call.
    """

    name = "seeded"

    def __init__(self, texts: list[str], *, cost: float = 0.0) -> None:
        self._texts = texts
        self._cost = cost
        self.calls = 0

    async def run(self, request: RunRequest, ctx: RunContext) -> RunResult:
        ctx.cancel_token.raise_if_cancelled()
        text = self._texts[self.calls % len(self._texts)]
        self.calls += 1
        return RunResult(text=text, model="seeded", cost_usd=self._cost)


# --------------------------------------------------------------------------- #
# Consensus (pure) — the vote itself.                                          #
# --------------------------------------------------------------------------- #


def test_majority_vote_picks_modal_output() -> None:
    rt = _SeededRuntime(["A", "A", "B", "A", "B"])
    quorum = QuorumRuntime(rt, k=5, early_stop=False)
    out = _run(quorum, _request())
    assert out.result.text == "A"
    assert out.consensus.tally == {'"A"': 3, '"B"': 2}
    assert out.consensus.runner_up_gap == 1


def test_majority_vote_field_collapses_key_order() -> None:
    """``majority_vote(field="label")`` collapses semantically-equal records to one key."""
    texts = [
        json.dumps({"a": 1, "b": 2, "label": "bug"}),
        json.dumps({"b": 2, "a": 1, "label": "bug"}),  # same label, different key order
        json.dumps({"label": "feature"}),
    ]
    rt = _SeededRuntime(texts)
    quorum = QuorumRuntime(rt, k=3, consensus=majority_vote(field="label"), early_stop=False)
    out = _run(quorum, _request())
    # The two reorderings vote together on "bug" -> 2 votes vs 1.
    assert json.loads(out.result.text)["label"] == "bug"
    assert out.consensus.tally == {'"bug"': 2, '"feature"': 1}


def test_high_cardinality_abstains_to_declared_default() -> None:
    """Every sample distinct ⇒ ill-defined plurality ⇒ abstain to the declared default."""
    rt = _SeededRuntime(["x1", "x2", "x3"])
    quorum = QuorumRuntime(rt, k=3, default_text="ABSTAIN", early_stop=False)
    out = _run(quorum, _request())
    assert out.result.text == "ABSTAIN"
    assert out.consensus.abstained is True


def test_abstention_without_default_raises() -> None:
    """No declared default ⇒ abstention raises, never a silent arbitrary pick."""
    rt = _SeededRuntime(["x1", "x2", "x3"])
    quorum = QuorumRuntime(rt, k=3, early_stop=False)
    with pytest.raises(QuorumAbstention):
        _run(quorum, _request())


# --------------------------------------------------------------------------- #
# Budget + bounds.                                                             #
# --------------------------------------------------------------------------- #


def test_budget_below_k_stops_early_never_exceeds() -> None:
    """A budget under k× per-call votes over the affordable prefix, never breaching."""
    rt = _SeededRuntime(["A", "A", "B", "B", "B"], cost=1.0)
    quorum = QuorumRuntime(rt, k=5, early_stop=False)
    # Ceiling of $2.50 affords exactly 2 leaves (the 3rd would need $3.00 > $2.50).
    ctx = RunContext(store=SqliteStore(), cost_budget=CostBudget(limit_usd=2.5))
    _sync(quorum.run_quorum(_request(), ctx))
    assert rt.calls == 2  # stopped before the unaffordable 3rd leaf
    assert ctx.cost_budget.spent_usd <= 2.5  # never exceeded the ceiling
    assert quorum.early_stops == 1


def test_budget_charge_never_raises_budget_exceeded() -> None:
    rt = _SeededRuntime(["A", "A", "A", "A", "A"], cost=1.0)
    quorum = QuorumRuntime(rt, k=5, early_stop=False)
    ctx = RunContext(store=SqliteStore(), cost_budget=CostBudget(limit_usd=3.0))
    try:
        _sync(quorum.run_quorum(_request(), ctx))
    except BudgetExceeded:  # pragma: no cover - asserts the negative
        pytest.fail("quorum must stop before charging past the ceiling")
    assert ctx.cost_budget.spent_usd <= 3.0


def test_each_sample_charges_the_shared_budget() -> None:
    rt = _SeededRuntime(["A", "A", "A"], cost=0.5)
    quorum = QuorumRuntime(rt, k=3, early_stop=False)
    ctx = RunContext(store=SqliteStore(), cost_budget=CostBudget(limit_usd=10.0))
    _sync(quorum.run_quorum(_request(), ctx))
    assert ctx.cost_budget.spent_usd == pytest.approx(1.5)  # 3 × $0.50


# --------------------------------------------------------------------------- #
# Sequential early-stop (F-8) + determinism.                                   #
# --------------------------------------------------------------------------- #


def test_sequential_test_stops_once_lead_is_real() -> None:
    """Unanimous early samples ⇒ the Wilson lower bound clears 0.5 ⇒ stop before k."""
    rt = _SeededRuntime(["A", "A", "A", "A", "A", "A", "A", "A", "A", "A"], cost=0.0)
    quorum = QuorumRuntime(rt, k=10, early_stop=True, min_k=3)
    out = _run(quorum, _request())
    assert out.result.text == "A"
    assert rt.calls < 10  # stopped early — did not draw all k
    assert quorum.early_stops == 1


def test_same_seed_identical_sample_count_and_winner() -> None:
    """Same base seed ⇒ identical sample count + winner (determinism)."""

    def _one() -> tuple[int, str]:
        rt = _SeededRuntime(["A", "A", "B", "A", "A"], cost=0.0)
        q = QuorumRuntime(rt, k=5, base_seed=42, early_stop=True, min_k=3)
        out = _run(q, _request())
        return rt.calls, out.result.text

    assert _one() == _one()


# --------------------------------------------------------------------------- #
# Cancellation.                                                                #
# --------------------------------------------------------------------------- #


def test_cancel_between_samples_is_honoured() -> None:
    rt = _SeededRuntime(["A", "A", "A", "A", "A"], cost=0.0)
    quorum = QuorumRuntime(rt, k=5, early_stop=False)
    ctx = RunContext(store=SqliteStore())
    ctx.cancel_token.cancel()
    from crawfish.core.context import Cancelled

    with pytest.raises(Cancelled):
        _sync(quorum.run_quorum(_request(), ctx))
    assert rt.calls == 0  # cancelled before the first leaf


# --------------------------------------------------------------------------- #
# Taint union (ALG-7) — a vote does not launder taint.                         #
# --------------------------------------------------------------------------- #


def test_winner_tainted_iff_any_sample_tainted() -> None:
    rt = _SeededRuntime(["A", "A", "B"])
    quorum = QuorumRuntime(rt, k=3, early_stop=False)
    # Fluid input ⇒ every sample tainted ⇒ aggregate tainted.
    tainted_out = _run(quorum, _request(fluid=True))
    assert tainted_out.tainted is True
    wrapped = quorum_output(tainted_out.result, produced_by="q", tainted=tainted_out.tainted)
    assert wrapped.tainted is True
    assert wrapped.value == "A"

    # No fluid input ⇒ no sample tainted ⇒ aggregate clean.
    rt2 = _SeededRuntime(["A", "A", "B"])
    quorum2 = QuorumRuntime(rt2, k=3, early_stop=False)
    clean_out = _run(quorum2, _request(fluid=False))
    assert clean_out.tainted is False
    clean_wrapped = quorum_output(clean_out.result, produced_by="q", tainted=clean_out.tainted)
    assert clean_wrapped.tainted is False


# --------------------------------------------------------------------------- #
# k defaulting from the tunable sample_k knob (AL-T1).                         #
# --------------------------------------------------------------------------- #


def test_k_defaults_to_definition_sample_k_knob() -> None:
    rt = _SeededRuntime(["A", "A", "A", "A"])
    # No explicit k; the Definition pins sample_k=4 ⇒ four samples.
    quorum = QuorumRuntime(rt, early_stop=False)
    _run(quorum, _request(sample_k=4))
    assert rt.calls == 4


# --------------------------------------------------------------------------- #
# F-1 record/replay: k distinct cassettes, majority replays bit-for-bit.      #
# --------------------------------------------------------------------------- #


def test_quorum_over_record_replay_k_distinct_cassettes(tmp_path: Path) -> None:
    """k=5 records 5 distinct cassettes; replay returns the majority bit-for-bit."""
    cassette_dir = tmp_path / "cassettes"
    # Record: the inner produces a majority "A" (3) vs "B" (2). decode_seed differs per
    # sample, so the inner can vary; we drive variation by call order via _SeededRuntime.
    inner = _SeededRuntime(["A", "A", "B", "A", "B"], cost=0.0)
    recorder = RecordReplayRuntime(inner, cassette_dir, record=True)
    quorum_rec = QuorumRuntime(recorder, k=5, base_seed=7, early_stop=False)
    out_rec = _run(quorum_rec, _request())
    assert out_rec.result.text == "A"

    # Exactly k distinct cassette files were written (no collision into one).
    cassettes = list(cassette_dir.glob("*.json"))
    assert len(cassettes) == 5

    # Replay (record=False, no inner calls): the recorded majority replays bit-for-bit.
    replay_inner = _SeededRuntime(["SHOULD-NOT-BE-CALLED"], cost=0.0)
    replayer = RecordReplayRuntime(replay_inner, cassette_dir, record=False)
    quorum_replay = QuorumRuntime(replayer, k=5, base_seed=7, early_stop=False)
    out_replay = _run(quorum_replay, _request())
    assert out_replay.result.text == out_rec.result.text == "A"
    assert replay_inner.calls == 0  # pure replay — no model call


def test_key_of_decodes_and_canonicalizes() -> None:
    """The consensus keyer canonicalises so reordered records share one vote key."""
    vote = MajorityVote(field="label")
    a = RunResult(text=json.dumps({"a": 1, "label": "bug", "b": 2}))
    b = RunResult(text=json.dumps({"b": 2, "label": "bug", "a": 1}))
    assert vote.key_of(a) == vote.key_of(b)


# --------------------------------------------------------------------------- #
# helpers                                                                      #
# --------------------------------------------------------------------------- #


def _run(quorum: QuorumRuntime, request: RunRequest):  # type: ignore[no-untyped-def]
    ctx = RunContext(store=SqliteStore())
    return _sync(quorum.run_quorum(request, ctx))


def _sync(coro):  # type: ignore[no-untyped-def]
    import asyncio

    return asyncio.run(coro)
