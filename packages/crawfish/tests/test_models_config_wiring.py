"""CRA-192: the configurable default model + named aliases wiring.

Asserts the single resolution path (``resolve_model`` + ``ModelsConfig``) is used
identically by the runtime and the cost preview, that an unpinned agent resolves to
the configured ``[models].default``, that aliases expand, and that with no config the
Claude-first ``DEFAULT_MODEL`` back-compat fallback is preserved (no drift).
"""

from __future__ import annotations

from crawfish.cost import estimate_cost
from crawfish.definition.types import AgentSpec, Definition, TeamSpec
from crawfish.provider import ModelsConfig
from crawfish.runtime.base import RunRequest
from crawfish.runtime.command import DEFAULT_MODEL, CommandRuntime


def _def(*models: str | None) -> Definition:
    agents = [AgentSpec(role=f"a{i}", model=m) for i, m in enumerate(models)]
    return Definition(team=TeamSpec(agents=agents))


def _runtime_resolved(definition: Definition, config: ModelsConfig | None) -> str:
    rt = CommandRuntime(config=config)
    req = RunRequest(definition=definition, role=definition.team.agents[0].role, inputs={})
    return rt._resolve_model(req)


# -- unpinned agent resolves to the configured default --------------------------
def test_unpinned_agent_uses_configured_default() -> None:
    d = _def(None)
    cfg = ModelsConfig(default="claude-sonnet-4-6")
    assert _runtime_resolved(d, cfg) == "claude-sonnet-4-6"


def test_unpinned_agent_back_compat_when_no_config() -> None:
    d = _def(None)
    # No config at all -> Claude-first DEFAULT_MODEL preserved (out-of-box behavior).
    assert _runtime_resolved(d, None) == DEFAULT_MODEL
    # Empty config (no default set) -> same back-compat fallback.
    assert _runtime_resolved(d, ModelsConfig()) == DEFAULT_MODEL


# -- alias expansion -----------------------------------------------------------
def test_alias_resolves_to_concrete_id() -> None:
    d = _def("fast")
    cfg = ModelsConfig(aliases={"fast": "claude-haiku-4-5"})
    assert _runtime_resolved(d, cfg) == "claude-haiku-4-5"


def test_per_run_override_still_wins() -> None:
    d = _def("fast")
    cfg = ModelsConfig(default="claude-sonnet-4-6", aliases={"fast": "claude-haiku-4-5"})
    rt = CommandRuntime(config=cfg)
    req = RunRequest(definition=d, role="a0", inputs={}, model="claude-opus-4-8")
    assert rt._resolve_model(req) == "claude-opus-4-8"


# -- cost preview reads the SAME config (no second source of truth) -------------
def test_cost_estimate_uses_same_config_as_runtime() -> None:
    # Unpinned + aliased agents; the estimate must resolve them via the same config.
    d = _def(None, "fast")
    cfg = ModelsConfig(
        default="claude-sonnet-4-6",
        aliases={"fast": "claude-haiku-4-5"},
    )
    est = estimate_cost(d, items=1, config=cfg)
    # default agent priced as sonnet, aliased agent priced as haiku — no DEFAULT_MODEL.
    assert set(est.per_model) == {"claude-sonnet-4-6", "claude-haiku-4-5"}
    assert DEFAULT_MODEL not in est.per_model
    # And it agrees with what the runtime would actually run.
    assert _runtime_resolved(_def(None), cfg) == "claude-sonnet-4-6"


def test_cost_estimate_back_compat_no_config() -> None:
    d = _def(None)
    est = estimate_cost(d, items=1)  # no config -> DEFAULT_MODEL price
    assert DEFAULT_MODEL in est.per_model
