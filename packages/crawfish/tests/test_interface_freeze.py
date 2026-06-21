"""CRA-184 — interface-freeze contract tests.

Asserts the Phase-2 contracts exist, are importable from the top-level package, are
shaped as downstream issues (#1/#2/#3/#4/#5/#6/#7/#8/#9/#10/#11/#12) expect, and that
the not-yet-implemented behavioural halves are honest stubs. No model calls — fully
deterministic.
"""

from __future__ import annotations

import dataclasses

import pytest
from pydantic import ValidationError as PydanticValidationError

import crawfish
from crawfish import (
    EMISSION_SCHEMA_VERSION,
    REQUIRED_ATTRS,
    Emission,
    EmissionKind,
    Grant,
    ModelsConfig,
    Provider,
    ProviderPolicy,
    StructuralDiff,
    ValidationError,
    ValidationFailure,
    resolve_model,
    structural_diff,
    validate_inputs,
    validate_output,
)


def test_all_contract_symbols_are_public() -> None:
    """Every frozen contract symbol is re-exported from the package root."""
    for name in (
        "Emission",
        "EmissionKind",
        "REQUIRED_ATTRS",
        "EMISSION_SCHEMA_VERSION",
        "ValidationFailure",
        "ValidationError",
        "StructuralDiff",
        "validate_output",
        "validate_inputs",
        "structural_diff",
        "Provider",
        "ProviderPolicy",
        "ModelsConfig",
        "resolve_model",
        "Grant",
    ):
        assert name in crawfish.__all__, f"{name} missing from crawfish.__all__"
        assert hasattr(crawfish, name)


# --- Emission taxonomy --------------------------------------------------------


def test_emission_kind_taxonomy_is_closed_and_covered() -> None:
    """Every EmissionKind has a REQUIRED_ATTRS entry, and the set matches exactly."""
    expected = {
        "run_start",
        "run_finish",
        "model",
        "tool",
        "sink",
        "compaction",
        "observer",
        "metric",
        "secret_lease",
        "jail_violation",
    }
    assert {k.value for k in EmissionKind} == expected
    # Required-attrs schema covers every kind (no kind left unspecified).
    assert set(REQUIRED_ATTRS.keys()) == set(EmissionKind)


def test_required_attrs_is_immutable() -> None:
    """The taxonomy schema cannot be mutated at runtime (drift guard)."""
    with pytest.raises(TypeError):
        REQUIRED_ATTRS[EmissionKind.MODEL] = ()  # type: ignore[index]


def test_emission_is_frozen_and_defaults_untainted() -> None:
    em = Emission(kind=EmissionKind.MODEL, run_id="r1", attrs={"model": "m", "cost_usd": 0.0})
    assert em.schema_version == EMISSION_SCHEMA_VERSION
    assert em.org_id == "local"
    assert em.tainted is False
    with pytest.raises(PydanticValidationError):
        em.attrs = {}  # type: ignore[misc]


def test_emission_missing_attrs_detects_incomplete_payload() -> None:
    incomplete = Emission(kind=EmissionKind.MODEL, run_id="r1", attrs={"model": "m"})
    assert incomplete.missing_attrs() == ("cost_usd",)
    assert incomplete.is_valid() is False

    complete = Emission(kind=EmissionKind.MODEL, run_id="r1", attrs={"model": "m", "cost_usd": 0.1})
    assert complete.missing_attrs() == ()
    assert complete.is_valid() is True


def test_emission_taint_propagation_flag_carried() -> None:
    tainted = Emission(kind=EmissionKind.TOOL, run_id="r1", attrs={"tool": "x"}, tainted=True)
    assert tainted.tainted is True


def test_emission_serialization_round_trips() -> None:
    # CRA-171 lands the behavioural half: to_event/from_event now round-trip.
    em = Emission(kind=EmissionKind.RUN_START, run_id="r1", attrs={"runtime": "mock"})
    event = em.to_event()
    assert event["kind"] == "run_start"
    assert Emission.from_event(event) == em


# --- Output validation contract ----------------------------------------------


def test_validation_failure_enum_closed() -> None:
    assert {f.value for f in ValidationFailure} == {
        "not_json",
        "missing_field",
        "type_mismatch",
        "extra_field",
        "empty_schema",
        "constraint",
    }


def test_validation_error_is_frozen() -> None:
    err = ValidationError(failure=ValidationFailure.MISSING_FIELD, field="title")
    assert err.detail == ""
    with pytest.raises(PydanticValidationError):
        err.detail = "x"  # type: ignore[misc]


def test_structural_diff_equal_predicate() -> None:
    assert StructuralDiff().equal is True
    assert StructuralDiff(changed=("a",)).equal is False


def test_output_validation_functions_implemented() -> None:
    """CRA-172 implements the frozen signatures (no longer NotImplementedError stubs)."""
    value, errors = validate_output("hello", [])  # no schema → pass-through string
    assert value == "hello" and errors == []
    assert validate_inputs({}, []) == []
    assert structural_diff(1, 2).changed == ("",)


# --- Provider / model resolution ---------------------------------------------


def test_resolve_model_str_list_none() -> None:
    assert resolve_model("claude-opus-4-8", default="d") == "claude-opus-4-8"
    assert resolve_model(["a", "b"], default="d") == "a"
    assert resolve_model([], default="d") == "d"
    assert resolve_model(None, default="d") == "d"


def test_resolve_model_uses_config_default_and_aliases() -> None:
    cfg = ModelsConfig(default="cfg-default", aliases={"fast": "claude-haiku-4-5"})
    assert resolve_model(None, default="d", config=cfg) == "cfg-default"
    assert resolve_model("fast", default="d", config=cfg) == "claude-haiku-4-5"
    # Alias expansion is single-hop; an unknown name passes through unchanged.
    assert resolve_model("unknown", default="d", config=cfg) == "unknown"
    # A pinned list's first entry is alias-expanded too.
    assert resolve_model(["fast"], default="d", config=cfg) == "claude-haiku-4-5"


def test_resolve_model_matches_legacy_call_sites() -> None:
    """The shared resolver reproduces the old CommandRuntime/cost behaviour exactly."""
    from crawfish.cost import _resolve_model as cost_resolve
    from crawfish.runtime.command import DEFAULT_MODEL

    assert cost_resolve(None) == DEFAULT_MODEL
    assert cost_resolve("m") == "m"
    assert cost_resolve(["x", "y"]) == "x"


def test_provider_policy_permits() -> None:
    assert ProviderPolicy().permits("anything") is True
    restricted = ProviderPolicy(allowed=("anthropic",))
    assert restricted.permits("anthropic") is True
    assert restricted.permits("openai") is False


def test_models_config_is_frozen() -> None:
    cfg = ModelsConfig()
    with pytest.raises(PydanticValidationError):
        cfg.default = "x"  # type: ignore[misc]


def test_provider_is_runtime_checkable_protocol() -> None:
    class _FakeProvider:
        name = "fake"

        def models(self) -> list[str]:
            return ["m"]

        def supports(self, model: str) -> bool:
            return model == "m"

        async def run(self, request: object, ctx: object) -> object:  # pragma: no cover
            return object()

    assert isinstance(_FakeProvider(), Provider)
    assert not isinstance(object(), Provider)


# --- Grant shape -------------------------------------------------------------


def test_grant_is_frozen_dataclass_with_predicates() -> None:
    g = Grant(package="pkg", secrets=("GH_TOKEN",), egress=("api.github.com",), granted_at=1.0)
    assert dataclasses.is_dataclass(g)
    assert g.permits_secret("GH_TOKEN") is True
    assert g.permits_secret("OTHER") is False
    assert g.permits_egress("api.github.com") is True
    assert g.grant_id  # auto-assigned
    with pytest.raises(dataclasses.FrozenInstanceError):
        g.package = "other"  # type: ignore[misc]
