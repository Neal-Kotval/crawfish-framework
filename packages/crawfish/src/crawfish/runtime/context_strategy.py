"""Context-window management — pluggable strategies.

A real run will exceed the context window. A ``ContextStrategy`` decides *when*
(threshold) and *how* (compaction) to shrink the transcript; it is applied inside the
Run loop. Built-ins are model-free and deterministic (a summary turn is a structural
digest), with an injectable summarizer for later model-backed compaction.

Injection boundary (load-bearing): if any compacted turn carried fluid data, the
resulting summary turn stays marked ``is_fluid_data`` — compacted data never silently
becomes instructions (taint propagation).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from crawfish.core.context import RunContext

if TYPE_CHECKING:
    from crawfish.runtime.context_artifact import Context

__all__ = [
    "ConversationTurn",
    "CompactionResult",
    "ContextStrategy",
    "MaxTokens",
    "LinearCompact",
    "ExponentialCompact",
    "Summarize",
    "estimate_tokens",
    "resolve_strategy",
    "manage_context",
    "DEFAULT_STRATEGY",
    # cross-agent context-carry strategies (Context artifact)
    "ContextCarryStrategy",
    "CarryFull",
    "CarryRecency",
    "CarrySummary",
    "CarryTypedFields",
    "resolve_carry_strategy",
    "DEFAULT_CARRY_STRATEGY",
]


def estimate_tokens(text: str) -> int:
    """Cheap, deterministic token estimate (~4 chars/token)."""
    return max(1, len(text) // 4)


class ConversationTurn(BaseModel):
    role: str
    text: str
    is_fluid_data: bool = False  # untrusted session data (injection boundary)

    @property
    def tokens(self) -> int:
        return estimate_tokens(self.text)


class CompactionResult(BaseModel):
    turns: list[ConversationTurn] = Field(default_factory=list)
    reclaimed_tokens: int = 0
    compacted: bool = False


def _total(turns: list[ConversationTurn]) -> int:
    return sum(t.tokens for t in turns)


def _summary_turn(turns: list[ConversationTurn]) -> ConversationTurn:
    """Deterministic digest of summarized turns; taints if any input was fluid data."""
    tainted = any(t.is_fluid_data for t in turns)
    reclaimed = _total(turns)
    return ConversationTurn(
        role="system",
        text=f"[compacted {len(turns)} turns, ~{reclaimed} tokens]",
        is_fluid_data=tainted,
    )


class ContextStrategy(ABC):
    name: str = "abstract"
    keep_recent: int = 4

    @abstractmethod
    def should_compact(self, turns: list[ConversationTurn]) -> bool: ...

    @abstractmethod
    def compact(self, turns: list[ConversationTurn]) -> CompactionResult: ...


class MaxTokens(ContextStrategy):
    """Hard windowing: drop oldest turns until under ``limit``. Prevents overflow."""

    name = "max_tokens"

    def __init__(self, limit: int = 200_000, keep_recent: int = 4) -> None:
        self.limit = limit
        self.keep_recent = keep_recent

    def should_compact(self, turns: list[ConversationTurn]) -> bool:
        return _total(turns) > self.limit

    def compact(self, turns: list[ConversationTurn]) -> CompactionResult:
        before = _total(turns)
        kept = list(turns)
        # drop from the front (oldest), never the most recent `keep_recent`
        while _total(kept) > self.limit and len(kept) > self.keep_recent:
            kept.pop(0)
        return CompactionResult(
            turns=kept, reclaimed_tokens=before - _total(kept), compacted=len(kept) != len(turns)
        )


class LinearCompact(ContextStrategy):
    """At a threshold, summarize the oldest turns into one, keep recent verbatim."""

    name = "linear_compact"

    def __init__(self, threshold: int = 150_000, keep_recent: int = 6) -> None:
        self.threshold = threshold
        self.keep_recent = keep_recent

    def should_compact(self, turns: list[ConversationTurn]) -> bool:
        return _total(turns) > self.threshold

    def compact(self, turns: list[ConversationTurn]) -> CompactionResult:
        if len(turns) <= self.keep_recent:
            return CompactionResult(turns=list(turns))
        before = _total(turns)
        old, recent = turns[: -self.keep_recent], turns[-self.keep_recent :]
        new = [_summary_turn(old), *recent]
        return CompactionResult(turns=new, reclaimed_tokens=before - _total(new), compacted=True)


class ExponentialCompact(ContextStrategy):
    """Progressively larger compactions: each pass keeps fewer recent turns."""

    name = "exponential_compact"

    def __init__(self, threshold: int = 150_000, keep_recent: int = 8) -> None:
        self.threshold = threshold
        self.keep_recent = keep_recent
        self._passes = 0

    def should_compact(self, turns: list[ConversationTurn]) -> bool:
        return _total(turns) > self.threshold

    def compact(self, turns: list[ConversationTurn]) -> CompactionResult:
        keep = max(2, self.keep_recent >> self._passes)
        self._passes += 1
        if len(turns) <= keep:
            return CompactionResult(turns=list(turns))
        before = _total(turns)
        old, recent = turns[:-keep], turns[-keep:]
        new = [_summary_turn(old), *recent]
        return CompactionResult(turns=new, reclaimed_tokens=before - _total(new), compacted=True)


class Summarize(ContextStrategy):
    """Summarize everything but the most recent turn at a threshold."""

    name = "summarize"

    def __init__(self, threshold: int = 100_000) -> None:
        self.threshold = threshold
        self.keep_recent = 1

    def should_compact(self, turns: list[ConversationTurn]) -> bool:
        return _total(turns) > self.threshold

    def compact(self, turns: list[ConversationTurn]) -> CompactionResult:
        if len(turns) <= 1:
            return CompactionResult(turns=list(turns))
        before = _total(turns)
        new = [_summary_turn(turns[:-1]), turns[-1]]
        return CompactionResult(turns=new, reclaimed_tokens=before - _total(new), compacted=True)


_REGISTRY: dict[str, type[ContextStrategy]] = {
    s.name: s for s in (MaxTokens, LinearCompact, ExponentialCompact, Summarize)
}
DEFAULT_STRATEGY = "linear_compact"  # compacts before overflow; sensible default


def resolve_strategy(name: str | None) -> ContextStrategy:
    cls = _REGISTRY.get(name or DEFAULT_STRATEGY)
    if cls is None:
        raise KeyError(f"unknown context strategy {name!r} (known: {sorted(_REGISTRY)})")
    return cls()


def manage_context(
    turns: list[ConversationTurn], strategy: ContextStrategy, ctx: RunContext
) -> list[ConversationTurn]:
    """Apply the strategy once: compact if over threshold, emit telemetry, return turns."""
    if not strategy.should_compact(turns):
        return turns
    result = strategy.compact(turns)
    if result.compacted:
        # A typed COMPACTION emission. If any compacted turn carried fluid data the
        # summary turn stays fluid, so propagate that taint across the emission
        # boundary (compacted untrusted data never silently becomes trusted).
        from crawfish.emission import Emission, EmissionKind, emit

        tainted = any(t.is_fluid_data for t in result.turns)
        emit(
            ctx.store,
            Emission(
                kind=EmissionKind.COMPACTION,
                run_id=ctx.run_id,
                org_id=ctx.org_id,
                attrs={
                    "strategy": strategy.name,
                    "turns_before": len(turns),
                    "turns_after": len(result.turns),
                    "reclaimed_tokens": result.reclaimed_tokens,
                },
                tainted=tainted,
            ),
            org_id=ctx.org_id,
        )
    return result.turns


# -- cross-agent context-carry strategies ----------------------------------
#
# The strategies above shrink a single agent's *transcript*. A
# ``ContextCarryStrategy`` decides what of the **Context artifact** (the typed,
# taint-aware state threaded between agents in team.py) to carry forward to the next
# agent. They are model-free and deterministic; an injectable summarizer is left as a
# later extension. Taint is load-bearing: a strategy that summarizes tainted entries
# produces a tainted summary entry — compacted untrusted data never becomes trusted.


class ContextCarryStrategy(ABC):
    """Decide which Context entries the next agent receives. Deterministic."""

    name: str = "abstract"

    @abstractmethod
    def carry(self, context: Context) -> Context:
        """Return a (possibly reduced) Context for the next agent."""


class CarryFull(ContextCarryStrategy):
    """Carry every entry forward verbatim (no reduction). The safe default."""

    name = "full"

    def carry(self, context: Context) -> Context:
        return context


class CarryRecency(ContextCarryStrategy):
    """Carry only the ``keep`` most-recently-produced entries (drop oldest)."""

    name = "recency"

    def __init__(self, keep: int = 3) -> None:
        self.keep = keep

    def carry(self, context: Context) -> Context:
        if len(context.entries) <= self.keep:
            return context
        kept = context.entries[-self.keep :]
        return context.model_copy(update={"entries": kept})


class CarryTypedFields(ContextCarryStrategy):
    """Carry only entries whose ``key`` is in an allow-list (typed-field projection).

    Lets a topology forward exactly the typed fields a downstream agent needs, dropping
    the rest. Types/taint/lineage on the kept entries are untouched.
    """

    name = "typed_fields"

    def __init__(self, fields: list[str] | None = None) -> None:
        self.fields = set(fields or [])

    def carry(self, context: Context) -> Context:
        if not self.fields:
            return context
        kept = [e for e in context.entries if e.key in self.fields]
        return context.model_copy(update={"entries": kept})


class CarrySummary(ContextCarryStrategy):
    """Collapse all entries into one deterministic ``summary`` entry.

    Taint survives compaction: the summary is tainted iff **any** collapsed entry was
    tainted (a summary of tainted content is tainted). Lineage is preserved when every
    collapsed entry shares one lineage, else dropped (it is no longer single-source).
    """

    name = "summary"

    def carry(self, context: Context) -> Context:
        from crawfish.runtime.context_artifact import ContextEntry

        if len(context.entries) <= 1:
            return context
        tainted = any(e.tainted for e in context.entries)
        lineages = {e.lineage for e in context.entries}
        lineage = next(iter(lineages)) if len(lineages) == 1 else None
        digest = {e.key: e.value for e in context.entries}
        summary = ContextEntry(
            key="summary",
            role="system",
            value=digest,
            tainted=tainted,
            lineage=lineage,
        )
        return context.model_copy(update={"entries": [summary]})


_CARRY_REGISTRY: dict[str, type[ContextCarryStrategy]] = {
    s.name: s for s in (CarryFull, CarryRecency, CarryTypedFields, CarrySummary)
}
DEFAULT_CARRY_STRATEGY = "full"  # lossless; opt into reduction explicitly


def resolve_carry_strategy(name: str | None) -> ContextCarryStrategy:
    cls = _CARRY_REGISTRY.get(name or DEFAULT_CARRY_STRATEGY)
    if cls is None:
        raise KeyError(
            f"unknown context-carry strategy {name!r} (known: {sorted(_CARRY_REGISTRY)})"
        )
    return cls()
