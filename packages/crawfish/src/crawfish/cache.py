"""Cost-aware replay caching — CRA-182's second lever.

:class:`~crawfish.runtime.replay.RecordReplayRuntime` already replays a recorded
``RunResult`` for free on a cache hit (no model call, no budget charge). CRA-182 makes
that *explicit and cost-aware*: a thin :class:`CachingRuntime` wrapper that reports, per
request, whether the call **hit** the cassette (and therefore avoided a spend) or
**missed** it — and totals the dollars the cache saved.

The cache key is the same definition-version + inputs hash the replay layer uses
(:func:`crawfish.runtime.replay._key`), surfaced here as :func:`cache_key` so callers can
reason about hit/miss without reaching into the runtime. Identical (definition-version +
inputs) calls collapse onto one cassette: the first records and spends, the rest hit and
cost $0.

Fully deterministic: the wrapper performs no model call itself; the inner replay runtime
does, only on a miss, and tests drive it with a mock inner runtime so nothing live runs.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

from crawfish.core.context import RunContext
from crawfish.runtime.base import AgentRuntime, RunRequest, RunResult
from crawfish.runtime.replay import RecordReplayRuntime, _key

__all__ = ["cache_key", "CacheStats", "CachingRuntime"]


def cache_key(request: RunRequest) -> str:
    """The cassette key for ``request`` — its definition-version + inputs hash.

    Re-exports the replay layer's :func:`crawfish.runtime.replay._key` so a caller can
    compute hit/miss (two requests share a key iff they would share a cassette) without
    depending on the private name. Pure: definition id + version, role, model, inputs,
    and session id, hashed deterministically.
    """
    return _key(request)


@dataclass
class CacheStats:
    """Running hit/miss + saved-spend accounting for a :class:`CachingRuntime`.

    ``hits``/``misses`` count requests served from / not from the cassette;
    ``saved_usd`` totals the spend each hit avoided (the recorded result's ``cost_usd``,
    which a miss would have charged). ``spent_usd`` totals what misses actually charged.
    """

    hits: int = 0
    misses: int = 0
    saved_usd: float = 0.0
    spent_usd: float = 0.0
    _seen_keys: set[str] = field(default_factory=set)

    @property
    def total(self) -> int:
        return self.hits + self.misses

    @property
    def hit_rate(self) -> float:
        """Fraction of requests served from cache (0.0 when nothing ran yet)."""
        return self.hits / self.total if self.total else 0.0


class CachingRuntime(AgentRuntime):
    """A cost-aware wrapper over :class:`RecordReplayRuntime`.

    Each :meth:`run` reports, via :attr:`stats`, whether the request hit the cassette
    (free, no budget charge — the saved spend is tallied) or missed it (the inner replay
    runtime records + the underlying model spends). A small in-process LRU of recently
    recorded results lets the wrapper price a hit even before the cassette is re-read,
    keeping ``saved_usd`` exact for repeated identical calls within a session.
    """

    name = "caching"

    def __init__(
        self,
        inner: RecordReplayRuntime,
        *,
        cassette_dir: str | Path | None = None,
        track_capacity: int = 1024,
    ) -> None:
        self._inner = inner
        # Reuse the replay runtime's own cassette dir unless overridden (read-only here).
        self._dir = Path(cassette_dir) if cassette_dir is not None else inner._dir
        self.stats = CacheStats()
        self._capacity = track_capacity
        # key -> recorded cost, so a within-session repeat is priced without re-reading.
        self._costs: OrderedDict[str, float] = OrderedDict()

    def _is_hit(self, request: RunRequest) -> bool:
        """True if a cassette already exists for this request (a free replay)."""
        return (self._dir / f"{_key(request)}.json").exists()

    def _remember(self, key: str, cost_usd: float) -> None:
        self._costs[key] = cost_usd
        self._costs.move_to_end(key)
        while len(self._costs) > self._capacity:
            self._costs.popitem(last=False)

    async def run(self, request: RunRequest, ctx: RunContext) -> RunResult:
        ctx.cancel_token.raise_if_cancelled()
        key = _key(request)
        hit = self._is_hit(request)
        result = await self._inner.run(request, ctx)

        if hit:
            # Replay charged nothing; tally the spend the cache avoided. Prefer the
            # within-session recorded cost (exact) over the replayed result's own.
            self.stats.hits += 1
            self.stats.saved_usd += self._costs.get(key, result.cost_usd)
        else:
            self.stats.misses += 1
            self.stats.spent_usd += result.cost_usd
            self._remember(key, result.cost_usd)
        self.stats._seen_keys.add(key)
        return result
