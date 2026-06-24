"""CRA-209 — AL-T1, the two-axis mode unifier (per-knob ``tunable`` + ``train()``/``eval()``).

Two orthogonal axes, unified:

* **Axis 1 (tunable)** — :class:`TuneSpec` / :class:`KnobDomain` as DATA: which knobs may
  move is content-hashable config (``tune.toml``), ``named_knobs()`` yields only tunable
  paths (sorted), and a TuneSpec-driven mutator refuses to move a ``tunable=False`` knob.
* **Axis 2 (mode)** — ``train()`` returns an unfrozen copy (fresh ``Version``); ``eval()``
  re-freezes to the canonical content sha (idempotent); a consequential side effect against
  an unfrozen Definition raises, against eval-mode succeeds.
"""

from __future__ import annotations

import pytest

from crawfish.definition.types import AgentSpec, Definition, TeamSpec
from crawfish.tuner import (
    KnobDomain,
    TuneSpec,
    eval,
    guard_consequential,
    train,
    tune_spec_sha,
)
from crawfish.versioning.version import FrozenError


def _base() -> Definition:
    return Definition(team=TeamSpec(agents=[AgentSpec(role="worker", model="slow")]))


# == Axis 1 — tunable as content-hashed data ================================
def test_named_knobs_yields_only_tunable_sorted() -> None:
    spec = TuneSpec(
        knobs=[
            KnobDomain(path="agent.worker.model", values=["fast", "slow"]),
            KnobDomain(path="agent.worker.prompt", values=["a"], tunable=False),
            KnobDomain(path="team.coordination", values=["single", "lead"]),
        ]
    )
    paths = [path for path, _ in spec.named_knobs()]
    # pinned 'prompt' excluded; remainder path-sorted (set/insertion-order-free).
    assert paths == ["agent.worker.model", "team.coordination"]
    assert spec.is_tunable("agent.worker.model") is True
    assert spec.is_tunable("agent.worker.prompt") is False
    assert spec.is_tunable("unknown.path") is False


def test_tune_spec_round_trips_through_toml() -> None:
    text = """
    [[knob]]
    path = "agent.worker.model"
    values = ["fast", "mid", "slow"]
    tunable = true

    [[knob]]
    path = "agent.worker.prompt"
    values = ["x"]
    tunable = false
    """
    spec = TuneSpec.from_toml(text)
    assert [k.path for k in spec.knobs] == ["agent.worker.model", "agent.worker.prompt"]
    assert spec.knobs[0].values == ["fast", "mid", "slow"]
    assert spec.knobs[1].tunable is False


def test_tune_spec_sha_changes_when_edited() -> None:
    a = TuneSpec(knobs=[KnobDomain(path="agent.worker.model", values=["fast", "slow"])])
    b = TuneSpec(knobs=[KnobDomain(path="agent.worker.model", values=["fast", "mid"])])
    assert tune_spec_sha(a) != tune_spec_sha(b)
    # Path order is normalized, so a re-ordered authoring hashes identically (stable sha).
    c = TuneSpec(
        knobs=[
            KnobDomain(path="b.x", values=[1]),
            KnobDomain(path="a.y", values=[2]),
        ]
    )
    d = TuneSpec(
        knobs=[
            KnobDomain(path="a.y", values=[2]),
            KnobDomain(path="b.x", values=[1]),
        ]
    )
    assert tune_spec_sha(c) == tune_spec_sha(d)


def test_empty_tune_spec_sha_is_stable() -> None:
    assert tune_spec_sha(TuneSpec()) == tune_spec_sha(TuneSpec(knobs=[]))


# == Axis 2 — train() / eval() mode =========================================
def test_train_returns_unfrozen_fresh_version() -> None:
    d = eval(_base())  # eval is the default: a frozen artifact
    assert d.frozen is True
    drafted = train(d)
    assert drafted.frozen is False  # train mode: mutable
    assert drafted.version.sha is None  # a fresh Version, not the eval sha
    # train() is copy-on-write — the original eval artifact is untouched.
    assert d.frozen is True


def test_eval_of_train_is_idempotent() -> None:
    d = eval(_base())
    round_tripped = eval(train(d))
    # eval(train(d)) re-hashes to d's eval sha when the knobs are unchanged.
    assert round_tripped.version.sha == d.version.sha
    assert round_tripped.frozen is True


def test_eval_rehash_diverges_on_knob_edit() -> None:
    d = eval(_base())
    drafted = train(d)
    # A real training mutation: change the model knob, then re-freeze.
    drafted.team.agents[0].model = "fast"
    re_eval = eval(drafted)
    assert re_eval.version.sha != d.version.sha  # distinct artifact, distinct sha


# == Load-bearing rule — consequential side effects are eval-only ===========
def test_guard_consequential_raises_in_train_mode() -> None:
    drafted = train(eval(_base()))
    with pytest.raises(FrozenError):
        guard_consequential(drafted)


def test_guard_consequential_passes_in_eval_mode() -> None:
    # No raise: an eval-mode (frozen) Definition may drive a consequential Sink / recorded run.
    guard_consequential(eval(_base()))


# == TuneSpec-driven mutation refuses a pinned knob =========================
def test_tune_spec_driven_mutator_refuses_pinned_knob() -> None:
    """A mutator that derives its moves from a TuneSpec proposes exactly the tunable knobs
    and refuses to mutate a ``tunable=False`` knob — the same set a hand-built mutator would.
    """
    spec = TuneSpec(
        knobs=[
            KnobDomain(path="agent.worker.model", values=["fast", "slow"]),
            KnobDomain(path="agent.worker.prompt", values=["nope"], tunable=False),
        ]
    )

    def spec_driven_models(s: TuneSpec) -> list[str]:
        for path, domain in s.named_knobs():
            if path == "agent.worker.model":
                return [str(v) for v in domain.values]
        return []

    # The TuneSpec-driven selection equals the hand-built model grid.
    assert spec_driven_models(spec) == ["fast", "slow"]
    # And the pinned prompt knob is simply never offered for mutation.
    assert "agent.worker.prompt" not in [p for p, _ in spec.named_knobs()]
    with pytest.raises(KeyError):
        # A caller that tries to move a pinned knob through the tunable map gets nothing.
        {p: d for p, d in spec.named_knobs()}["agent.worker.prompt"]
