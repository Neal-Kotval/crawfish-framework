"""EscalatingRuntime — a confidence-gated model cascade.

Static routing (:class:`~crawfish.runtime.routing_runtime.RoutingRuntime`) picks a model
*before* the call from the definition/policy. This is the **outcome-based** complement:
run a cheap primary model first, inspect the result, and re-run on a stronger model only
when the primary is unsure (or its output won't parse). Most items finish on the cheap
model; only the hard tail pays for the strong one — a better point on the cost/quality
frontier than pinning either model for the whole batch.

It composes with any inner runtime by **pinning ``request.model``** per attempt (a
concrete id resolves to itself downstream), so a single :class:`CommandRuntime` drives
both tiers against the same ``claude -p`` backend. Each attempt charges and emits through
the inner runtime exactly as a normal call, so the cost budget and the event ledger see
both tiers; an escalated item simply shows two ``MODEL`` emissions on its run.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable

from crawfish.core.context import RunContext
from crawfish.runtime.base import AgentRuntime, RunRequest, RunResult

__all__ = ["EscalatingRuntime", "confidence_below"]

# Predicate over a primary RunResult: True ⇒ escalate to the strong model.
EscalationPredicate = Callable[[RunResult], bool]

_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")


def _extract_field(text: str, field: str) -> float | None:
    """Best-effort: parse JSON out of ``text`` and read a numeric ``field`` from it."""
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        obj = json.loads(text[start : end + 1])
    except (ValueError, TypeError):
        return None
    if not isinstance(obj, dict) or field not in obj:
        return None
    raw = obj[field]
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, str):
        m = _NUM_RE.search(raw)
        if m:
            return float(m.group())
    return None


def confidence_below(
    threshold: float,
    *,
    field: str = "confidence",
    escalate_on_unparseable: bool = True,
) -> EscalationPredicate:
    """Escalate when the primary's self-reported ``confidence`` is below ``threshold``.

    An output with no readable confidence field escalates by default
    (``escalate_on_unparseable``) — an unparseable cheap answer is exactly the case a
    stronger model should re-attempt.
    """

    def predicate(result: RunResult) -> bool:
        value = _extract_field(result.text, field)
        if value is None:
            return escalate_on_unparseable
        return value < threshold

    return predicate


class EscalatingRuntime(AgentRuntime):
    """Run ``primary_model``; on a positive predicate, re-run on ``strong_model``."""

    name = "escalating"

    def __init__(
        self,
        inner: AgentRuntime,
        *,
        primary_model: str,
        strong_model: str,
        should_escalate: EscalationPredicate,
    ) -> None:
        self._inner = inner
        self._primary = primary_model
        self._strong = strong_model
        self._should = should_escalate
        # Aggregate counters across the runtime's lifetime (handy for batch reporting).
        self.calls = 0
        self.escalations = 0

    async def run(self, request: RunRequest, ctx: RunContext) -> RunResult:
        ctx.cancel_token.raise_if_cancelled()
        first = await self._inner.run(request.model_copy(update={"model": self._primary}), ctx)
        self.calls += 1
        if not self._should(first):
            return first
        ctx.cancel_token.raise_if_cancelled()
        strong = await self._inner.run(request.model_copy(update={"model": self._strong}), ctx)
        self.calls += 1
        self.escalations += 1
        return strong
