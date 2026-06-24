"""CRA-198 / F-5 acceptance: decode-knob ownership.

Settles WHO owns each decode parameter (ADR 0017):
  * tunable knobs (temperature/top_p/sample_k) live on the Definition/AgentSpec and
    enter the content hash — the ONE authoritative location;
  * RunRequest.temperature is DERIVED from the resolved Definition, never set
    independently (exactly one authoritative location, the other derived);
  * grammar/decode_seed are per-call RunRequest fields, kept OUT of the content hash;
  * AgentRuntime advertises a DeterminismTier capability.
"""

from __future__ import annotations

from enum import Enum

from crawfish.definition.types import (
    CONTENT_HASH_VERSION,
    DECODE_KNOB_FIELDS,
    AgentSpec,
    Definition,
    TeamSpec,
)
from crawfish.runtime.base import AgentRuntime, DeterminismTier, RunRequest


def _defn(**knobs: float | int | None) -> Definition:
    return Definition(
        team=TeamSpec(agents=[AgentSpec(role="lead", **knobs)], lead="lead"),
    )


# -- AC1: temperature has exactly ONE authoritative location -----------------


def test_temperature_lives_on_the_spec_and_flows_to_the_request() -> None:
    d = _defn(temperature=0.7)
    req = RunRequest(definition=d, role="lead")
    # The request value is DERIVED from the resolved Definition.
    assert req.temperature == 0.7
    assert req.resolved_decode() == {"temperature": 0.7}


def test_request_temperature_is_derived_not_independently_settable() -> None:
    d = _defn(temperature=0.3)
    req = RunRequest(definition=d, role="lead")
    # `temperature` is a read-only derivation, not a settable field: there is no way
    # to pin a conflicting independent value on the request that could drift from the
    # content-hashed Definition value.
    assert "temperature" not in RunRequest.model_fields
    assert req.temperature == 0.3  # always reflects the resolved spec
    # Mutating the authoritative source (the spec) is the only way to change it.
    d.team.agents[0].temperature = 0.9
    assert req.temperature == 0.9


def test_unpinned_temperature_is_none() -> None:
    req = RunRequest(definition=_defn(), role="lead")
    assert req.temperature is None
    assert req.resolved_decode() == {}


def test_resolved_decode_defaults_to_lead_then_first_agent() -> None:
    d = Definition(
        team=TeamSpec(
            agents=[AgentSpec(role="a", temperature=0.1), AgentSpec(role="b", temperature=0.2)],
        )
    )
    # No role and no lead -> first agent.
    assert RunRequest(definition=d).resolved_decode() == {"temperature": 0.1}
    # Explicit role wins.
    assert RunRequest(definition=d, role="b").resolved_decode() == {"temperature": 0.2}


# -- AC2: re-freeze / content-hash behavior ----------------------------------


def test_none_knobs_are_hash_neutral() -> None:
    """A Definition with the new fields absent/None hashes byte-identically to the
    pre-field payload: the knob keys are dropped from the canonical hash dict."""
    d = _defn()
    payload = d.content_dict()
    agent0 = payload["team"]["agents"][0]  # type: ignore[index]
    for name in DECODE_KNOB_FIELDS:
        assert name not in agent0  # excluded when None -> unmigrated sha unchanged


def test_setting_a_knob_changes_the_sha() -> None:
    base = _defn().content_sha()
    hot = _defn(temperature=0.8).content_sha()
    assert hot != base  # a pinned knob enters the hash and diverges
    # Distinct knob values -> distinct shas; identical values -> identical sha.
    assert _defn(temperature=0.8).content_sha() == hot
    assert _defn(top_p=0.9).content_sha() != base
    assert _defn(sample_k=40).content_sha() != base


def test_content_sha_is_deterministic() -> None:
    assert _defn(temperature=0.5).content_sha() == _defn(temperature=0.5).content_sha()


def test_content_hash_version_is_recorded() -> None:
    assert CONTENT_HASH_VERSION == 1


# -- AC3: grammar / decode_seed are per-call and OUT of the hash -------------


def test_grammar_and_decode_seed_default_none() -> None:
    req = RunRequest(definition=_defn(), role="lead")
    assert req.grammar is None
    assert req.decode_seed is None


def test_grammar_and_decode_seed_are_settable_per_call() -> None:
    req = RunRequest(definition=_defn(), role="lead", grammar="json", decode_seed=42)
    assert req.grammar == "json"
    assert req.decode_seed == 42


def test_grammar_and_decode_seed_do_not_enter_the_definition_hash() -> None:
    # They live on the request, not the Definition — the Definition's content sha
    # cannot be perturbed by a per-call grammar or seed.
    d = _defn(temperature=0.7)
    sha_before = d.content_sha()
    RunRequest(definition=d, role="lead", grammar="regex:.*", decode_seed=99)
    assert d.content_sha() == sha_before
    # And the knob payload carries no grammar/seed key.
    assert "grammar" not in d.content_dict()
    assert "decode_seed" not in d.content_dict()


# -- AC4: DeterminismTier capability -----------------------------------------


def test_determinism_tier_is_str_enum() -> None:
    assert issubclass(DeterminismTier, str)
    assert issubclass(DeterminismTier, Enum)
    assert {t.value for t in DeterminismTier} == {"honors-seed", "best-effort", "none"}


def test_runtime_advertises_a_tier_with_a_safe_default() -> None:
    # The ABC default keeps every existing runtime valid without a code change.
    assert AgentRuntime.determinism_tier is DeterminismTier.BEST_EFFORT


def test_runtime_can_override_its_tier() -> None:
    class _Seeded(AgentRuntime):
        determinism_tier = DeterminismTier.HONORS_SEED

        async def run(self, request, ctx):  # type: ignore[no-untyped-def]
            raise NotImplementedError

    assert _Seeded().determinism_tier is DeterminismTier.HONORS_SEED
