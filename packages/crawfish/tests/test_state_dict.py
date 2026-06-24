"""CRA-210 — AL-T2: state_dict() / load_state() references-by-version transfer.

The architecture/weights split (R5, "Hugging-Face-for-agent-weights"). These tests pin the
load-bearing guarantees:

* ``state_dict()`` carries the tunable knobs only — it EXCLUDES architecture keys (team
  topology, IO schema, dependencies);
* editing a knob changes ``StateDict.sha`` (the weights identity);
* ``d.load_state(d.state_dict())`` is sha-identity (CoW re-mints the same content sha);
* ``strict=True`` raises on a shape mismatch; ``strict=False`` loads the intersection;
* ``only=[...]`` transfers a chosen knob group only (e.g. few-shots);
* summons travel as ``{id, version}`` references-by-version, never an embedded Definition;
* a loaded state is a NEW frozen Definition (copy-on-write); only static knobs move.
"""

from __future__ import annotations

import pytest

from crawfish.core.types import Flow, Parameter
from crawfish.definition.types import (
    AgentSpec,
    Coordination,
    Definition,
    DefinitionRef,
    Prompt,
    TeamSpec,
)
from crawfish.learning import (
    IncompatibleStateError,
    StateDict,
    load_state,
    state_dict,
)
from crawfish.tuner import eval as eval_mode


def _defn(
    *,
    prompt: str = "do it",
    model: str = "slow",
    temperature: float | None = None,
    deps: list[DefinitionRef] | None = None,
    coordination: Coordination = Coordination.SINGLE,
    extra_input: bool = False,
) -> Definition:
    inputs = [Parameter(name="task", type="text", flow=Flow.FLUID)]
    if extra_input:
        inputs.append(Parameter(name="ctx", type="text", flow=Flow.STATIC))
    return Definition(
        team=TeamSpec(
            agents=[AgentSpec(role="worker", prompt=prompt, model=model, temperature=temperature)],
            coordination=coordination,
        ),
        inputs=inputs,
        dependencies=deps or [],
    )


# -- state_dict carries the knobs, excludes architecture ---------------------
def test_state_dict_excludes_architecture_keys() -> None:
    d = _defn(deps=[DefinitionRef(id="sub-1", version="0.1")])
    state = state_dict(d)

    assert set(state.roles) == {"worker"}
    assert state.roles["worker"].prompt == "do it"
    assert state.roles["worker"].model == "slow"
    # summons are references-by-version, not an embedded Definition
    assert state.summons == [DefinitionRef(id="sub-1", version="0.1")]
    # architecture (IO schema, role topology) is NOT in the knob payload
    dumped = state.model_dump(mode="json")
    assert "inputs" not in dumped and "outputs" not in dumped
    assert "dependencies" not in dumped
    assert "team" not in dumped


# -- editing a knob changes the weights sha ----------------------------------
def test_editing_a_knob_changes_sha() -> None:
    base = state_dict(_defn(model="slow"))
    changed = state_dict(_defn(model="fast"))
    assert base.sha != changed.sha
    # an identical extraction re-hashes to the same sha (deterministic)
    assert base.sha == state_dict(_defn(model="slow")).sha


# -- a knob edit does NOT change the structure sha (still transfer-compatible) --
def test_knob_edit_keeps_structure_sha() -> None:
    a = state_dict(_defn(model="slow"))
    b = state_dict(_defn(model="fast"))
    assert a.structure_sha == b.structure_sha  # same architecture
    # but an IO-schema change DOES change the structure sha
    c = state_dict(_defn(model="slow", extra_input=True))
    assert c.structure_sha != a.structure_sha


# -- load_state(state_dict()) is sha-identity --------------------------------
def test_load_state_roundtrip_is_sha_identity() -> None:
    d = eval_mode(_defn(model="mid"))
    reloaded = load_state(d, state_dict(d))
    assert reloaded.frozen
    assert reloaded.content_sha() == d.content_sha()
    assert reloaded.version.sha == d.content_sha()


# -- strict=True raises on a shape mismatch ----------------------------------
def test_load_state_strict_rejects_incompatible_structure() -> None:
    source = _defn(model="fast")
    target = _defn(model="slow", extra_input=True)  # different IO schema
    with pytest.raises(IncompatibleStateError):
        load_state(target, state_dict(source), strict=True)


# -- strict=False loads the structural intersection --------------------------
def test_load_state_non_strict_loads_intersection() -> None:
    # source has roles {worker, helper}; target has only {worker}. The shared role's knob
    # transfers; the missing role is simply skipped (no error).
    source = Definition(
        team=TeamSpec(
            agents=[
                AgentSpec(role="worker", prompt="src-worker", model="fast"),
                AgentSpec(role="helper", prompt="src-helper", model="fast"),
            ]
        ),
        inputs=[Parameter(name="task", type="text", flow=Flow.FLUID)],
    )
    target = _defn(prompt="tgt", model="slow")  # role {worker} only, different IO is fine
    loaded = load_state(target, state_dict(source), strict=False)
    assert loaded.agent("worker").prompt == "src-worker"
    assert loaded.agent("worker").model == "fast"
    assert loaded.agent("helper") is None  # not invented


# -- only=[...] transfers a chosen knob group only ---------------------------
def test_load_state_only_fewshots() -> None:
    source = _defn(prompt="src-prompt", model="fast")
    source = source.model_copy(
        update={"injected_prompts": [Prompt(target="worker", text="Examples: ...")]}
    )
    target = _defn(prompt="keep-me", model="slow")
    loaded = load_state(target, state_dict(source), only=["fewshots"])
    # few-shots transferred ...
    assert [p.text for p in loaded.injected_prompts] == ["Examples: ..."]
    # ... but prompt/model were NOT (only=["fewshots"] excludes them)
    assert loaded.agent("worker").prompt == "keep-me"
    assert loaded.agent("worker").model == "slow"


def test_load_state_only_model() -> None:
    source = _defn(prompt="src-prompt", model="fast")
    target = _defn(prompt="keep-prompt", model="slow")
    loaded = load_state(target, state_dict(source), only=["model"])
    assert loaded.agent("worker").model == "fast"  # transferred
    assert loaded.agent("worker").prompt == "keep-prompt"  # untouched


# -- coordination is a tunable knob, transfers when requested ----------------
def test_load_state_transfers_coordination() -> None:
    source = _defn(coordination=Coordination.SEQUENTIAL)
    target = _defn(coordination=Coordination.SINGLE)
    loaded = load_state(target, state_dict(source), strict=False)
    assert loaded.team.coordination is Coordination.SEQUENTIAL


# -- decode knobs transfer (and are hash-neutral when None) ------------------
def test_load_state_transfers_decode_knobs() -> None:
    source = _defn(temperature=0.7)
    target = _defn(temperature=None)
    loaded = load_state(target, state_dict(source), strict=False)
    assert loaded.agent("worker").temperature == 0.7


# -- copy-on-write: the target is never mutated in place ---------------------
def test_load_state_is_copy_on_write() -> None:
    target = eval_mode(_defn(model="slow"))
    target_sha_before = target.content_sha()
    source = _defn(model="fast")
    loaded = load_state(target, state_dict(source), strict=False)
    # new frozen artifact, distinct sha; the target is unchanged
    assert loaded.version.sha != target.version.sha
    assert target.content_sha() == target_sha_before
    assert target.agent("worker").model == "slow"


# -- embedding a nested Definition as a summon is rejected (JSON-only) --------
def test_summons_reject_embedded_definition() -> None:
    with pytest.raises(Exception):  # noqa: B017,PT011 — pydantic validation error type varies
        StateDict(summons=[_defn()])  # type: ignore[list-item]
