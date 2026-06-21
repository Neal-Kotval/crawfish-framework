"""CRA-182 acceptance — cost-aware replay caching.

Identical (definition-version + inputs) calls collapse onto one cassette: the first
records and spends; the rest HIT and cost $0. The ``CachingRuntime`` makes the hit/miss
and the avoided spend explicit. Deterministic: a ``MockProvider`` with a fixed cost is
the only "model"; no live call, no egress.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from crawfish.cache import CachingRuntime, cache_key
from crawfish.core.context import RunContext
from crawfish.definition.types import AgentSpec, Definition, TeamSpec
from crawfish.runtime import MockProvider, ProviderRuntime, RecordReplayRuntime, RunRequest
from crawfish.store import SqliteStore


def _ctx() -> RunContext:
    return RunContext(store=SqliteStore())


def _definition() -> Definition:
    return Definition(team=TeamSpec(agents=[AgentSpec(role="scout", prompt="scan")]))


def _request(d: Definition, pr_body: str) -> RunRequest:
    return RunRequest(definition=d, role="scout", inputs={"pr_body": pr_body})


def _caching(tmp_path: Path) -> CachingRuntime:
    # Inner: a $0.05/turn provider wrapped in record-replay (records on a miss).
    inner = ProviderRuntime([MockProvider("p", ["m1"], cost_usd=0.05)], default_model="m1")
    replay = RecordReplayRuntime(inner, tmp_path / "cassettes", record=True)
    return CachingRuntime(replay)


async def test_identical_call_hits_cache_and_avoids_second_spend(tmp_path: Path) -> None:
    rt = _caching(tmp_path)
    d = _definition()

    ctx1 = _ctx()
    await rt.run(_request(d, "same body"), ctx1)
    # First call: a miss — the inner provider spends $0.05.
    assert rt.stats.misses == 1 and rt.stats.hits == 0
    assert ctx1.cost_budget.spent_usd == pytest.approx(0.05)

    ctx2 = _ctx()
    await rt.run(_request(d, "same body"), ctx2)
    # Second identical call: a HIT — replayed for free, no budget charge.
    assert rt.stats.hits == 1 and rt.stats.misses == 1
    assert ctx2.cost_budget.spent_usd == pytest.approx(0.0)
    # The cache tallied the avoided spend.
    assert rt.stats.saved_usd == pytest.approx(0.05)
    assert rt.stats.spent_usd == pytest.approx(0.05)
    assert rt.stats.hit_rate == pytest.approx(0.5)


async def test_different_inputs_miss_separately(tmp_path: Path) -> None:
    rt = _caching(tmp_path)
    d = _definition()
    await rt.run(_request(d, "body A"), _ctx())
    await rt.run(_request(d, "body B"), _ctx())
    # Distinct inputs -> distinct cassettes -> two misses, no saving.
    assert rt.stats.misses == 2 and rt.stats.hits == 0
    assert rt.stats.saved_usd == pytest.approx(0.0)


def test_cache_key_collapses_identical_requests() -> None:
    d = _definition()
    assert cache_key(_request(d, "x")) == cache_key(_request(d, "x"))
    assert cache_key(_request(d, "x")) != cache_key(_request(d, "y"))
