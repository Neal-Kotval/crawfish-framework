"""CRA-185 acceptance: the shared Phase-2 fixture & determinism harness.

Covers the four scope items:

1. Canned per-provider ``stream-json`` fed to ``CommandRuntime`` via a transport stub.
2. Prompt-injection fixtures (fluid inputs / untrusted tool results).
3. Recorded LLM-judge / tuner runs (deterministic, no live call).
4. The taint-propagation conformance suite (Output ``derive`` + Emission, incl. the
   CRA-184 tool-result-tainted rule).

Everything here is deterministic — no subprocess, no network, no wall clock.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from crawfish.core.context import RunContext
from crawfish.core.types import Flow, Parameter
from crawfish.definition import AgentSpec, Definition, TeamSpec
from crawfish.emission import Emission, EmissionKind, read_emissions
from crawfish.eval import LLMJudge
from crawfish.output import Output
from crawfish.runtime.base import EventKind, RunRequest
from crawfish.runtime.command import CommandRuntime
from crawfish.store import SqliteStore
from crawfish.testing import (
    INJECTION_INPUTS,
    STREAM_FIXTURES,
    TaintCase,
    assert_taint_conformance,
    canned_transport,
    injection_tool_result,
    load_stream_fixture,
    replaying,
    scoring_runtime,
    taint_conformance_cases,
)

PROVIDERS = ["anthropic", "openai", "gemini", "local"]


def _ctx() -> RunContext:
    return RunContext(store=SqliteStore())


def _judge_definition() -> Definition:
    return Definition(
        team=TeamSpec(agents=[AgentSpec(role="judge", prompt="Score the output.")]),
        inputs=[
            Parameter(name="output", type="str"),  # fluid: the thing being judged
            Parameter(name="criteria", type="str", flow=Flow.STATIC),
        ],
    )


# -- 1. canned per-provider stream-json -------------------------------------
@pytest.mark.parametrize("provider", PROVIDERS)
async def test_canned_transport_parses_each_provider(provider: str) -> None:
    stream = load_stream_fixture(f"{provider}_clean")
    rt = CommandRuntime(transport=canned_transport(stream))
    d = _judge_definition()
    result = await rt.run(RunRequest(definition=d, role="judge", inputs={"output": "x"}), _ctx())
    # The canned result line is parsed; no subprocess, no live call.
    assert result.text.startswith("{") and "category" in result.text
    assert result.session_id is not None
    assert any(e.kind is EventKind.RESULT for e in result.events)


def test_stream_fixtures_dir_is_populated() -> None:
    found = {p.stem for p in STREAM_FIXTURES.glob("*.jsonl")}
    for provider in PROVIDERS:
        assert f"{provider}_clean" in found


async def test_canned_transport_is_deterministic() -> None:
    stream = load_stream_fixture("anthropic_clean")
    rt = CommandRuntime(transport=canned_transport(stream))
    d = _judge_definition()
    a = await rt.run(RunRequest(definition=d, role="judge", inputs={"output": "x"}), _ctx())
    b = await rt.run(RunRequest(definition=d, role="judge", inputs={"output": "x"}), _ctx())
    assert a.text == b.text and a.cost_usd == b.cost_usd


# -- 2. prompt-injection fixtures -------------------------------------------
def test_injection_inputs_available() -> None:
    assert "ignore_instructions" in INJECTION_INPUTS
    assert all(isinstance(v, str) and v for v in INJECTION_INPUTS.values())


async def test_injection_fluid_input_never_reaches_instructions() -> None:
    # An injection payload bound as a *fluid* input must land only inside the
    # fenced untrusted-data block, never the instruction half of the prompt.
    captured: dict[str, str] = {}

    async def spy_transport(_args: list[str], prompt: str) -> str:
        captured["prompt"] = prompt
        return load_stream_fixture("anthropic_clean")

    d = _judge_definition()
    rt = CommandRuntime(transport=spy_transport)
    payload = injection_tool_result("ignore_instructions")
    await rt.run(RunRequest(definition=d, role="judge", inputs={"output": payload}), _ctx())

    prompt = captured["prompt"]
    instructions, _, data_block = prompt.partition("UNTRUSTED DATA")
    assert "UNTRUSTED DATA" in prompt  # the fluid value was fenced
    assert payload not in instructions  # never in the instruction half
    assert "Ignore all previous instructions" not in instructions


async def test_injection_stream_fixture_carries_untrusted_tool_result() -> None:
    stream = load_stream_fixture("anthropic_injection")
    rt = CommandRuntime(transport=canned_transport(stream))
    # The fixture includes a tool_result with an injection attempt; it parses cleanly.
    result = await rt.run(
        RunRequest(definition=_judge_definition(), role="judge", inputs={"output": "x"}), _ctx()
    )
    assert any(e.kind is EventKind.TOOL_RESULT for e in result.events)
    assert "IGNORE ALL PREVIOUS INSTRUCTIONS" in stream
    assert "tool_result" in stream


# -- 3. recorded LLM-judge / tuner runs -------------------------------------
async def test_scoring_runtime_is_deterministic_judge() -> None:
    judge = LLMJudge(_judge_definition(), scoring_runtime(0.75))
    out: Output[object] = Output(output_schema=[], value={"text": "ok"}, produced_by="n")
    score_a = await judge.grade(out, _ctx())
    score_b = await judge.grade(out, _ctx())
    assert score_a == score_b == 0.75


async def test_scoring_runtime_replays_through_cassette(tmp_path: Path) -> None:
    # record once from the deterministic backend, then replay with no inner call.
    inner = scoring_runtime(0.4)
    rec = replaying(inner, tmp_path / "judge", record=True)
    d = _judge_definition()
    req = RunRequest(definition=d, role="judge", inputs={"output": "x"})
    recorded = await rec.run(req, _ctx())

    replay = replaying(scoring_runtime(0.99), tmp_path / "judge", record=False)
    played = await replay.run(req, _ctx())  # cassette hit -> ignores the 0.99 inner
    assert played.text == recorded.text  # the recorded 0.4 verdict, not 0.99


# -- 4. taint-propagation conformance suite ---------------------------------
def test_taint_conformance_suite_passes() -> None:
    # The reusable suite #1/#4/#9 reference: green across every boundary.
    assert_taint_conformance()


def test_taint_cases_cover_the_load_bearing_rows() -> None:
    by_name = {c.name: c for c in taint_conformance_cases()}
    # fluid input -> tainted
    assert by_name["fluid_input"].expected is True
    # static-only input + tool result -> tainted (CRA-184)
    assert by_name["static_plus_tool"].source_tainted is False
    assert by_name["static_plus_tool"].from_tool is True
    assert by_name["static_plus_tool"].expected is True
    # static-only, no tool -> clean (the only untainted row)
    assert by_name["static_no_tool"].expected is False


def test_fluid_input_taints_output_and_emission() -> None:
    # fluid input -> tainted Output -> tainted Emission
    fluid = Output(output_schema=[], value="untrusted", produced_by="src", tainted=True)
    derived = fluid.derive(value={"summary": "s"}, produced_by="node")
    assert derived.tainted is True
    emission = Emission(
        kind=EmissionKind.MODEL,
        run_id="r",
        attrs={"model": "m", "cost_usd": 0.0},
        tainted=derived.tainted,
    )
    assert emission.tainted is True


def test_tool_derived_emission_must_be_tainted() -> None:
    # CRA-184: static-only input + tool result -> tainted emission of kind TOOL.
    static = Output(output_schema=[], value="cfg", produced_by="src", tainted=False)
    derived = static.derive(value=injection_tool_result(), produced_by="node", tainted=True)
    assert derived.tainted is True
    emission = Emission(
        kind=EmissionKind.TOOL, run_id="r", attrs={"tool": "fetch"}, tainted=derived.tainted
    )
    assert emission.kind is EmissionKind.TOOL and emission.tainted is True


def test_tainted_emission_round_trips_through_ledger() -> None:
    # The taint marker survives the Emission ledger boundary (#1 acceptance).
    store = SqliteStore()
    from crawfish.emission import emit

    emit(
        store,
        Emission(kind=EmissionKind.TOOL, run_id="run-x", attrs={"tool": "t"}, tainted=True),
    )
    back = read_emissions(store, "run-x")
    assert len(back) == 1 and back[0].tainted is True


def test_conformance_detects_a_broken_boundary() -> None:
    # A case whose declared expectation contradicts taint must fail loudly, proving
    # the suite actually checks (a static+tool row that claims it stays clean).
    broken = TaintCase("bad", source_tainted=False, from_tool=True, expected=False)
    with pytest.raises(AssertionError):
        assert_taint_conformance([broken])
