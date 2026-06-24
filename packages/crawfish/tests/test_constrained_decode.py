"""CRA-218 / TS-8 acceptance: constrained / grammar-guided decoding.

Decode-time constraint is strictly stronger than the post-hoc validate+``_repair`` loop:
the runtime is told the output *shape* up front, so a malformed output becomes an
impossible state rather than a retried failure. The acceptance criteria:

  * a grammar-honouring run produces a structured field and **never enters ``_repair``**
    (``repair_count == 0`` where the unconstrained run would have been > 0);
  * ``decode_seed`` makes the constrained decode reproducible (same seed ⇒ same result);
  * the grammar projection is **pure and deterministic** (same text + grammar ⇒ same out);
  * a constrained vs unconstrained decode differ predictably;
  * a schema-bearing step can opt out with ``grammar=None`` (the reasoning caveat);
  * ``grammar``/``decode_seed`` are per-call and stay OUT of the Definition content hash;
  * the grammar is static/trusted — a fluid value cannot set it.

All deterministic, no live model calls (MockRuntime / pure functions).
"""

from __future__ import annotations

from crawfish.core.context import RunContext
from crawfish.core.types import Flow, JSONValue, Parameter
from crawfish.definition import AgentSpec, Definition, TeamSpec
from crawfish.grammar import Grammar, GrammarError, GrammarKind, parse
from crawfish.run import Run, RunStatus
from crawfish.runtime.base import RunRequest
from crawfish.runtime.mock import MockRuntime
from crawfish.store import SqliteStore


def _ctx() -> RunContext:
    return RunContext(store=SqliteStore())


# --- 1. Grammar is a pure, deterministic projection -------------------------


def test_enum_enforce_snaps_to_a_declared_choice() -> None:
    g = Grammar.enum(["triage", "ignore", "escalate"])
    # already-valid passes through; an off-set token snaps to the first declared member.
    assert g.enforce("escalate") == "escalate"
    assert g.enforce("garbage") == "triage"
    # a substring match snaps to that choice, deterministically (declared order wins).
    assert g.enforce("please ignore this") == "ignore"
    assert g.satisfies("triage") and not g.satisfies("nope")


def test_regex_enforce_extracts_first_match_or_raises() -> None:
    g = Grammar.regex(r"\d{3}")
    assert g.enforce("123") == "123"
    assert g.enforce("code=456 ok") == "456"
    # genuinely unsatisfiable text → GrammarError (never a silent coercion).
    try:
        g.enforce("no digits here")
        raise AssertionError("expected GrammarError")
    except GrammarError:
        pass


def test_json_enforce_keeps_declared_keys_and_drops_the_rest() -> None:
    g = Grammar.json_object(["score", "label"])
    out = g.enforce('{"score": 5, "label": "hot", "extra": "x"}')
    # declared keys kept, undeclared dropped, canonical (sorted) serialization.
    assert out == '{"label":"hot","score":5}'
    # a JSON object wrapped in prose is recovered (the parse-failure repair eliminated).
    wrapped = g.enforce('Sure! Here is the result: {"score": 9, "label": "warm"} — done.')
    assert wrapped == '{"label":"warm","score":9}'


def test_enforce_is_deterministic() -> None:
    g = Grammar.enum(["a", "b", "c"])
    assert g.enforce("zzz") == g.enforce("zzz") == "a"
    gj = Grammar.json_object(["x", "y"])
    assert gj.enforce('{"y": 2, "x": 1}') == gj.enforce('{"x": 1, "y": 2}')


# --- 2. Serialization round-trips (the per-call dialect string) -------------


def test_grammar_round_trips_through_the_request_string() -> None:
    for g in (
        Grammar.enum(["yes", "no", "maybe"]),
        Grammar.regex(r"^\d{4}-\d{2}$"),
        Grammar.json_object(["a", "b", "c"]),
    ):
        s = g.to_request_grammar()
        back = parse(s)
        assert back == g
        assert back.to_request_grammar() == s


def test_parse_rejects_a_malformed_dialect_string() -> None:
    for bad in ("no-separator", "bogus:body"):
        try:
            parse(bad)
            raise AssertionError(f"expected GrammarError for {bad!r}")
        except GrammarError:
            pass


# --- 3. Derive a (trusted) grammar from the declared output schema ----------


def test_from_output_schema_builds_a_json_grammar_for_a_structured_schema() -> None:
    g = Grammar.from_output_schema(
        [Parameter(name="score", type="int"), Parameter(name="label", type="str")]
    )
    assert g is not None and g.kind is GrammarKind.JSON_SCHEMA
    assert g.keys == ("score", "label")
    # free-text / empty schemas have nothing to constrain → None (caller leaves grammar off).
    assert Grammar.from_output_schema([Parameter(name="t", type="str")]) is None
    assert Grammar.from_output_schema([]) is None


# --- 4. Security: the grammar is static/trusted, OUT of the content hash -----


def _structured_defn() -> Definition:
    """A Definition with a structured (non-str) output schema + a fluid input."""
    return Definition(
        team=TeamSpec(agents=[AgentSpec(role="lead", prompt="classify")], lead="lead"),
        inputs=[Parameter(name="ticket", type="str", flow=Flow.FLUID)],
        outputs=[Parameter(name="score", type="int"), Parameter(name="label", type="str")],
    )


def test_grammar_and_seed_do_not_perturb_the_definition_hash() -> None:
    d = _structured_defn()
    sha_before = d.content_sha()
    g = Grammar.from_output_schema(list(d.outputs))
    assert g is not None
    # Putting the grammar on the per-call request must not move the Definition sha (F-5).
    RunRequest(definition=d, role="lead", grammar=g.to_request_grammar(), decode_seed=7)
    assert d.content_sha() == sha_before
    assert "grammar" not in d.content_dict()
    assert "decode_seed" not in d.content_dict()


def test_grammar_is_derived_from_static_schema_not_a_fluid_value() -> None:
    # The grammar is built from the declared output schema (author config / static),
    # never from the fluid ticket body. A fluid value has no path to set the constraint.
    d = _structured_defn()
    g = Grammar.from_output_schema(list(d.outputs))
    assert g is not None
    # The constraint keys come from the schema, independent of any (untrusted) input.
    assert g.keys == ("score", "label")


# --- 5. Constrained run produces a structured field and never repairs --------


def _grammar_aware_responder(request: RunRequest) -> str:
    """A backend that *honours a grammar* when one is supplied, else emits free prose.

    The deterministic stand-in for a constrained-decode backend: given a
    ``request.grammar`` it parses the supplied (trusted) constraint and emits a conforming
    object; with no grammar it returns unstructured prose the validator rejects as NOT_JSON
    (→ a metered repair). The same request always yields the same text."""
    if request.grammar is not None:
        constraint = parse(request.grammar)
        if constraint.kind is GrammarKind.JSON_SCHEMA:
            return '{"score": 3, "label": "low"}'
    return "I think this ticket is low priority, score around three."


async def test_constrained_run_produces_a_valid_field_with_zero_repairs() -> None:
    d = _structured_defn()
    g = Grammar.from_output_schema(list(d.outputs))
    run = Run(
        d,
        {"ticket": "printer on fire"},
        grammar=g,
        decode_seed=42,
        validate_input_types=False,
    )
    out = await run.execute(_ctx(), MockRuntime(_grammar_aware_responder))
    assert run.status is RunStatus.DONE
    # The grammar steered the backend to a well-formed object → validation passed with
    # NO repair (the parse-failure path is dead code under constrained decode).
    assert run.repair_count == 0
    assert isinstance(out.value, dict)
    assert set(out.value) == {"score", "label"}


async def test_unconstrained_same_backend_repairs() -> None:
    # The SAME backend with NO grammar emits prose → NOT_JSON → the configured REPAIR
    # action pays a metered re-prompt (repair_count > 0). This is the exact cost the
    # constrained path above eliminates.
    from crawfish.validation import ValidationAction

    d = _structured_defn()
    run = Run(
        d,
        {"ticket": "printer on fire"},
        on_invalid=ValidationAction.REPAIR,
        validate_input_types=False,
    )
    try:
        await run.execute(_ctx(), MockRuntime(_grammar_aware_responder))
    except Exception:
        pass  # the repair re-prompt (no grammar) still emits prose and fails — fine
    assert run.repair_count > 0  # it DID pay for a repair the constrained path avoids


# --- 6. Constrained vs unconstrained differ predictably ----------------------


async def test_constrained_vs_unconstrained_differ_predictably() -> None:
    # An enum-constrained classifier: the backend emits free text; constrained decode
    # snaps it onto the declared label set, unconstrained keeps the raw text.
    d = Definition(
        team=TeamSpec(agents=[AgentSpec(role="lead", prompt="label")], lead="lead"),
        inputs=[Parameter(name="ticket", type="str", flow=Flow.FLUID)],
        outputs=[Parameter(name="label", type="str")],
    )
    g = Grammar.enum(["bug", "feature", "question"])

    def responder(_request: RunRequest) -> str:
        return "this looks like a feature request to me"

    unconstrained = Run(d, {"ticket": "x"}, validate_input_types=False)
    u_out = await unconstrained.execute(_ctx(), MockRuntime(responder))

    constrained = Run(d, {"ticket": "x"}, grammar=g, validate_input_types=False)
    c_out = await constrained.execute(_ctx(), MockRuntime(responder))

    assert c_out.value == "feature"  # snapped to the declared member
    assert u_out.value != c_out.value  # unconstrained kept the raw prose
    assert "feature request" in str(u_out.value)


# --- 7. decode_seed makes the constrained decode reproducible ----------------


async def test_same_seed_same_constrained_result() -> None:
    d = _structured_defn()
    g = Grammar.from_output_schema(list(d.outputs))

    def seeded(request: RunRequest) -> str:
        # A seed-driven backend: same seed ⇒ same emitted payload.
        return f'{{"score": {request.decode_seed}, "label": "x"}}'

    results: list[JSONValue] = []
    for _ in range(2):
        run = Run(d, {"ticket": "t"}, grammar=g, decode_seed=99, validate_input_types=False)
        out = await run.execute(_ctx(), MockRuntime(seeded))
        results.append(out.value)
    assert results[0] == results[1]
    assert run.repair_count == 0


# --- 8. A schema-bearing step can opt out with grammar=None ------------------


async def test_schema_bearing_step_can_opt_out_with_grammar_none() -> None:
    # The "Let Me Speak Freely?" caveat: a reasoning-heavy step keeps grammar=None even
    # though the schema could yield one. It then takes the ordinary (unconstrained) path.
    d = Definition(
        team=TeamSpec(agents=[AgentSpec(role="lead", prompt="reason")], lead="lead"),
        inputs=[Parameter(name="ticket", type="str", flow=Flow.FLUID)],
        outputs=[Parameter(name="label", type="str")],
    )
    run = Run(d, {"ticket": "x"}, grammar=None, validate_input_types=False)
    out = await run.execute(_ctx(), MockRuntime())
    assert run.status is RunStatus.DONE
    assert run.grammar is None  # opted out — no constraint applied
    assert isinstance(out.value, str)
