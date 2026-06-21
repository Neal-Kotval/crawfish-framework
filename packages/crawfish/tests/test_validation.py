"""CRA-172 acceptance: typed input/output validation, structured outputs, diffs.

Deterministic: no live model calls — all parsing/validation is pure, and the run-path
tests drive a :class:`~crawfish.runtime.mock.MockRuntime` with fixed responders.
"""

from __future__ import annotations

import pytest

from crawfish.core.context import CostBudget, RunContext
from crawfish.core.types import Flow, Parameter
from crawfish.definition import AgentSpec, Definition, TeamSpec
from crawfish.run import InputValidationError, Run, RunStatus
from crawfish.runtime.mock import MockRuntime
from crawfish.store import SqliteStore
from crawfish.typesystem import TypeRegistry
from crawfish.validation import (
    StructuralDiff,
    ValidationAction,
    ValidationFailure,
    canonicalize,
    structural_diff,
    validate_inputs,
    validate_output,
)


def _registry() -> TypeRegistry:
    reg = TypeRegistry()
    reg.register_record("Triage", {"category": "str", "severity": "str", "summary": "str"})
    reg.register_record("Item", {"name": "str", "qty": "int"})
    return reg


def _ctx(store: SqliteStore | None = None, **kw: object) -> RunContext:
    return RunContext(store=store or SqliteStore(), **kw)  # type: ignore[arg-type]


# -- validate_output: typed values -----------------------------------------
def test_record_output_yields_typed_dict() -> None:
    reg = _registry()
    out = [Parameter(name="triage", type="Triage")]
    text = '{"severity": "high", "category": "bug", "summary": "x"}'
    value, errors = validate_output(text, out, reg)
    assert errors == []
    assert value == {"category": "bug", "severity": "high", "summary": "x"}
    # canonicalised: keys sorted regardless of input order
    assert list(value.keys()) == ["category", "severity", "summary"]


def test_list_output() -> None:
    reg = _registry()
    out = [Parameter(name="items", type="list[Item]")]
    text = '[{"name": "a", "qty": 1}, {"name": "b", "qty": 2}]'
    value, errors = validate_output(text, out, reg)
    assert errors == []
    assert value == [{"name": "a", "qty": 1}, {"name": "b", "qty": 2}]


def test_primitive_int_output() -> None:
    value, errors = validate_output("42", [Parameter(name="n", type="int")])
    assert errors == [] and value == 42


def test_str_output_is_passthrough_not_json() -> None:
    # A single str output stays raw text (claude -p has no JSON mode).
    value, errors = validate_output("just some prose", [Parameter(name="t", type="str")])
    assert errors == [] and value == "just some prose"


def test_no_schema_is_passthrough() -> None:
    value, errors = validate_output("anything", [])
    assert errors == [] and value == "anything"


def test_extracts_json_from_code_fence_and_prose() -> None:
    reg = _registry()
    out = [Parameter(name="triage", type="Triage")]
    text = 'Here you go:\n```json\n{"category":"bug","severity":"low","summary":"s"}\n```\nDone.'
    value, errors = validate_output(text, out, reg)
    assert errors == [] and value["category"] == "bug"


# -- validate_output: failures ---------------------------------------------
def test_missing_required_field() -> None:
    reg = _registry()
    out = [Parameter(name="triage", type="Triage")]
    value, errors = validate_output('{"category": "bug", "severity": "high"}', out, reg)
    assert any(
        e.failure is ValidationFailure.MISSING_FIELD and e.field == "summary" for e in errors
    )


def test_type_mismatch() -> None:
    reg = _registry()
    out = [Parameter(name="items", type="list[Item]")]
    # A single declared output IS the whole value, so the path is rooted there.
    value, errors = validate_output('[{"name": "a", "qty": "lots"}]', out, reg)
    assert any(
        e.failure is ValidationFailure.TYPE_MISMATCH and e.field == "[0].qty" for e in errors
    )


def test_unparseable_is_not_json() -> None:
    reg = _registry()
    out = [Parameter(name="triage", type="Triage")]
    value, errors = validate_output("no json at all here", out, reg)
    assert [e.failure for e in errors] == [ValidationFailure.NOT_JSON]
    assert value == "no json at all here"  # raw text preserved


def test_multiple_outputs_keyed_by_name() -> None:
    out = [Parameter(name="score", type="int"), Parameter(name="label", type="str")]
    value, errors = validate_output('{"label": "ok", "score": 7}', out)
    assert errors == [] and value == {"label": "ok", "score": 7}


# -- validate_inputs --------------------------------------------------------
def test_validate_inputs_accepts_well_typed() -> None:
    schema = [Parameter(name="repo", type="str"), Parameter(name="n", type="int")]
    assert validate_inputs({"repo": "acme/app", "n": 3}, schema) == []


def test_validate_inputs_rejects_wrong_type() -> None:
    schema = [Parameter(name="n", type="int")]
    errors = validate_inputs({"n": "not-a-number"}, schema)
    assert [e.failure for e in errors] == [ValidationFailure.TYPE_MISMATCH]
    assert errors[0].field == "n"


def test_validate_inputs_missing_required() -> None:
    schema = [Parameter(name="repo", type="str", required=True)]
    errors = validate_inputs({}, schema)
    assert [e.failure for e in errors] == [ValidationFailure.MISSING_FIELD]


# -- structural_diff --------------------------------------------------------
def test_diff_record_added_removed_changed() -> None:
    before = {"a": 1, "b": 2, "c": 3}
    after = {"a": 1, "b": 9, "d": 4}
    diff = structural_diff(before, after)
    assert diff.changed == ("b",)
    assert diff.added == ("d",)
    assert diff.removed == ("c",)
    assert diff.equal is False


def test_diff_list_elementwise() -> None:
    diff = structural_diff([1, 2, 3], [1, 5, 3, 4])
    assert diff.changed == ("[1]",)
    assert diff.added == ("[3]",)
    assert diff.removed == ()


def test_diff_nested_dotted_paths() -> None:
    before = {"x": {"y": 1}}
    after = {"x": {"y": 2}}
    assert structural_diff(before, after).changed == ("x.y",)


def test_diff_equal_when_same() -> None:
    assert structural_diff({"a": 1}, {"a": 1}).equal is True
    assert isinstance(structural_diff(1, 1), StructuralDiff)


# -- canonicalisation -------------------------------------------------------
def test_canonicalization_key_order_irrelevant() -> None:
    a = {"b": 1, "a": {"d": 4, "c": 3}}
    b = {"a": {"c": 3, "d": 4}, "b": 1}
    assert canonicalize(a) == canonicalize(b)
    assert structural_diff(a, b).equal is True
    # and parsed outputs compare equal regardless of key order
    out = [Parameter(name="m", type="json")]
    v1, _ = validate_output('{"b":1,"a":2}', out)
    v2, _ = validate_output('{"a":2,"b":1}', out)
    assert v1 == v2


# -- run.py integration -----------------------------------------------------
def _typed_def() -> tuple[Definition, TypeRegistry]:
    reg = _registry()
    d = Definition(
        team=TeamSpec(agents=[AgentSpec(role="main", prompt="triage")]),
        inputs=[
            Parameter(name="project", type="str", flow=Flow.STATIC),
            Parameter(name="ticket_body", type="str"),
        ],
        outputs=[Parameter(name="triage", type="Triage")],
    )
    return d, reg


def _good_payload() -> str:
    return '{"category": "bug", "severity": "high", "summary": "broken"}'


async def test_run_produces_typed_record_value() -> None:
    d, reg = _typed_def()
    run = Run(d, {"project": "acme", "ticket_body": "x"}, registry=reg)
    out = await run.execute(_ctx(), MockRuntime(responder=lambda _r: _good_payload()))
    assert run.status is RunStatus.DONE
    assert isinstance(out.value, dict)  # typed, not a string
    assert out.value["category"] == "bug"
    assert out.tainted is True  # fluid ticket_body taints the typed value


async def test_run_rejects_wrong_typed_input_before_model_call() -> None:
    d, reg = _typed_def()
    calls = {"n": 0}

    def responder(_r: object) -> str:
        calls["n"] += 1
        return _good_payload()

    # ticket_body declared str but bound an int → rejected before any run.
    run = Run(d, {"project": "acme", "ticket_body": 123}, registry=reg)
    with pytest.raises(InputValidationError):
        await run.execute(_ctx(), MockRuntime(responder=responder))
    assert calls["n"] == 0  # no model call happened


async def test_repair_reprompts_and_respects_budget() -> None:
    d, reg = _typed_def()
    calls = {"n": 0}

    def responder(_r: object) -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            return "totally not json"  # first attempt fails validation
        return _good_payload()  # repair attempt succeeds

    run = Run(
        d,
        {"project": "acme", "ticket_body": "x"},
        on_invalid=ValidationAction.REPAIR,
        registry=reg,
    )
    # Mock costs 0; a tiny budget proves REPAIR's extra call is metered through charge().
    ctx = _ctx(cost_budget=CostBudget(limit_usd=1.0))
    out = await run.execute(ctx, MockRuntime(responder=responder))
    assert out.value["category"] == "bug"
    assert calls["n"] == 2  # exactly one repair re-prompt, no more
    assert ctx.cost_budget.spent_usd <= 1.0  # never exceeds the budget


async def test_repair_skips_extra_call_when_budget_exhausted() -> None:
    """REPAIR must not spawn a metered re-prompt with no budget headroom left."""
    d, reg = _typed_def()
    calls = {"n": 0}

    def responder(_r: object) -> str:
        calls["n"] += 1
        return "not json"  # always invalid

    run = Run(
        d,
        {"project": "acme", "ticket_body": "x"},
        on_invalid=ValidationAction.REPAIR,
        registry=reg,
    )
    # Budget already fully spent (remaining == 0) -> repair pre-flight dead-letters.
    ctx = _ctx(cost_budget=CostBudget(limit_usd=1.0, spent_usd=1.0))
    with pytest.raises(Exception):  # noqa: B017 - OutputValidationError is internal
        await run.execute(ctx, MockRuntime(responder=responder))
    assert run.status is RunStatus.FAILED
    assert calls["n"] == 1  # the first attempt only; no repair re-prompt


async def test_dead_letter_default_raises_on_invalid_output() -> None:
    d, reg = _typed_def()
    run = Run(d, {"project": "acme", "ticket_body": "x"}, registry=reg)  # default DEAD_LETTER
    with pytest.raises(Exception):  # noqa: B017 - OutputValidationError is internal
        await run.execute(_ctx(), MockRuntime(responder=lambda _r: "not json"))
    assert run.status is RunStatus.FAILED


async def test_tool_result_run_is_tainted_even_with_static_inputs() -> None:
    from crawfish.runtime.base import (
        AgentRuntime,
        EventKind,
        RunRequest,
        RunResult,
        RuntimeEvent,
    )

    class ToolRuntime(AgentRuntime):
        name = "tool-mock"

        async def run(self, request: RunRequest, ctx: RunContext) -> RunResult:
            return RunResult(
                text="ok",
                events=[
                    RuntimeEvent(kind=EventKind.TOOL_RESULT, text="untrusted tool output"),
                    RuntimeEvent(kind=EventKind.RESULT, text="ok"),
                ],
            )

    d = Definition(
        team=TeamSpec(agents=[AgentSpec(role="main", prompt="do")]),
        inputs=[Parameter(name="repo", type="str", flow=Flow.STATIC)],  # all static
        outputs=[Parameter(name="t", type="str")],
    )
    out = await Run(d, {"repo": "acme/app"}).execute(_ctx(), ToolRuntime())
    assert out.tainted is True  # tool result is an injection vector → tainted
