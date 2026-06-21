"""The anomaly engine — *act* on the typed emission stream (CRA-190).

CRA-171 produces a typed :class:`~crawfish.emission.Emission` stream and CRA-181
*renders* it; this module is the **acted-upon** half. A small, deterministic rule
engine consumes the emission stream and, on a breach, escalates through a **tiered
response**:

* :attr:`Response.FLAG`  — emit an :class:`~crawfish.observe.ObserverEvent`
  (``Severity.WARN``); a visible finding, nothing stops.
* :attr:`Response.ALERT` — same, raised to ``Severity.CRITICAL``.
* :attr:`Response.HALT`  — the runaway kill-switch: trip the run's
  :class:`~crawfish.core.context.CancelToken` (cooperative kill) and force the
  :class:`~crawfish.core.context.CostBudget` over its ceiling so the next
  ``charge`` raises :class:`~crawfish.core.context.BudgetExceeded`.

This is the safety backstop the security spine assumes and the ceiling the Tuner
(CRA-176) and learning loop (CRA-177) need: unbounded autonomous model spend must be
haltable. Rules run in the **orchestrator**, never the jailed child (CRA-179), so a
compromised agent cannot disable them.

Security / taint: a HALT decision must not be spoofable. Rules act only on **typed,
structural signals** carried in :attr:`Emission.attrs` — cost, failure counts, rates,
emission volume, run age — never on free-text fluid content. An emission's
:attr:`Emission.tainted` marker is recorded on the resulting finding for the
dashboard, but untrusted content can never *itself* instruct a halt or a bypass: the
numeric thresholds are evaluated by code the agent cannot reach.

Determinism: every rule reads ``ts`` from the emissions (or an explicit ``now``
passed by the caller). No rule reads a wall clock in a way that affects its outcome,
so a seeded synthetic stream FLAGs/ALERTs/HALTs identically every run.

Example::

    engine = AnomalyEngine([
        CostSpikeRule(threshold_usd=2.0, response=Response.HALT),
        FailureRateRule(threshold=0.5, window="-1h", response=Response.ALERT),
        EmissionFloodRule(max_count=500, response=Response.HALT),
    ])
    firings = engine.guard(ctx, emissions)   # trips ctx.cancel_token on a HALT breach
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

from crawfish.core.context import BudgetExceeded
from crawfish.emission import Emission, EmissionKind
from crawfish.observe import ObserverEvent, ObserverSurface, Severity, parse_since

if TYPE_CHECKING:
    from crawfish.core.context import RunContext
    from crawfish.store.base import Store

__all__ = [
    "Response",
    "AnomalyRule",
    "CostSpikeRule",
    "FailureRateRule",
    "StuckRunRule",
    "EmissionFloodRule",
    "BudgetApproachingRule",
    "Firing",
    "AnomalyEngine",
    "read_and_guard",
]

# A HALT forces the budget over its ceiling. With an unbounded (limit_usd=None)
# budget there is no numeric ceiling to trip, so we set one a hair below current
# spend; the next charge then raises BudgetExceeded regardless.
_HALT_BUDGET_EPSILON = 1e-9


class Response(str, Enum):
    """The tier a breached rule escalates to. Ordered FLAG < ALERT < HALT."""

    FLAG = "flag"  # emit a WARN finding; nothing stops
    ALERT = "alert"  # emit a CRITICAL finding; nothing stops
    HALT = "halt"  # trip CancelToken + force BudgetExceeded (the kill-switch)

    @property
    def severity(self) -> Severity:
        """The :class:`Severity` an :class:`ObserverEvent` for this tier carries."""
        return Severity.WARN if self is Response.FLAG else Severity.CRITICAL

    @property
    def halts(self) -> bool:
        """Whether this tier trips the run's kill-switch."""
        return self is Response.HALT


def _window_emissions(emissions: Sequence[Emission], window: str, now: float) -> list[Emission]:
    """Emissions whose ``ts`` falls within ``window`` (e.g. ``"-5m"``) of ``now``.

    Deterministic: the threshold is computed from the caller-supplied ``now``, never a
    wall clock. A ``window`` of ``""``/``None``-equivalent (epoch 0) means "everything".
    """
    threshold = parse_since(window, now=now)
    return [e for e in emissions if e.ts >= threshold]


def _any_tainted(emissions: Sequence[Emission]) -> bool:
    return any(e.tainted for e in emissions)


def _pipeline_of(emissions: Sequence[Emission]) -> str:
    return next((e.pipeline for e in emissions if e.pipeline), "unknown")


class AnomalyRule(ABC):
    """A deterministic check over the emission stream. Returns a :class:`Firing` or ``None``.

    Subclasses read only **typed/structural** signals from :attr:`Emission.attrs`
    (cost, counts, rates, volume, age) — never free-text fluid content — so a HALT
    decision can never be spoofed by untrusted input.
    """

    kind: str

    def __init__(self, *, response: Response = Response.FLAG) -> None:
        self.response = response

    @abstractmethod
    def evaluate(
        self, emissions: Sequence[Emission], *, now: float, pipeline: str | None = None
    ) -> Firing | None: ...

    def _fire(
        self,
        detail: str,
        data: dict[str, object],
        *,
        now: float,
        pipeline: str | None,
        emissions: Sequence[Emission],
    ) -> Firing:
        run_id = next((e.run_id for e in emissions if e.run_id), None)
        event = ObserverEvent(
            pipeline=pipeline or _pipeline_of(emissions),
            kind=self.kind,
            severity=self.response.severity,
            detail=detail,
            observer=f"anomaly:{self.kind}",
            run_id=run_id,
            ts=now,
            data={**data, "response": self.response.value},
        )
        # Record (but never trust) the taint provenance of the window for the dashboard.
        return Firing(
            rule=self, response=self.response, event=event, tainted=_any_tainted(emissions)
        )


class CostSpikeRule(AnomalyRule):
    """Breach when summed ``cost_usd`` across MODEL emissions in ``window`` ≥ ``threshold_usd``."""

    kind = "cost.spike"

    def __init__(
        self, *, threshold_usd: float, window: str = "-5m", response: Response = Response.FLAG
    ) -> None:
        super().__init__(response=response)
        self.threshold_usd = threshold_usd
        self.window = window

    def evaluate(
        self, emissions: Sequence[Emission], *, now: float, pipeline: str | None = None
    ) -> Firing | None:
        window = _window_emissions(emissions, self.window, now)
        spent = sum(
            _as_float(e.attrs.get("cost_usd")) for e in window if e.kind is EmissionKind.MODEL
        )
        if spent < self.threshold_usd:
            return None
        return self._fire(
            f"${spent:.2f} model spend in {self.window.lstrip('-')} (≥ ${self.threshold_usd:.2f})",
            {"spent_usd": spent, "threshold_usd": self.threshold_usd},
            now=now,
            pipeline=pipeline,
            emissions=window,
        )


class FailureRateRule(AnomalyRule):
    """Breach when the fraction of failed RUN_FINISH emissions in ``window`` > ``threshold``."""

    kind = "failure.rate"

    def __init__(
        self, *, threshold: float, window: str = "-1h", response: Response = Response.FLAG
    ) -> None:
        super().__init__(response=response)
        self.threshold = threshold
        self.window = window

    def evaluate(
        self, emissions: Sequence[Emission], *, now: float, pipeline: str | None = None
    ) -> Firing | None:
        finishes = [
            e
            for e in _window_emissions(emissions, self.window, now)
            if e.kind is EmissionKind.RUN_FINISH
        ]
        if not finishes:
            return None
        failed = sum(1 for e in finishes if str(e.attrs.get("status")) == "failed")
        rate = failed / len(finishes)
        if rate <= self.threshold:
            return None
        return self._fire(
            f"{failed}/{len(finishes)} runs failed ({rate:.0%} > {self.threshold:.0%})",
            {"rate": rate, "failed": failed, "total": len(finishes)},
            now=now,
            pipeline=pipeline,
            emissions=finishes,
        )


class StuckRunRule(AnomalyRule):
    """Breach when a run has a RUN_START but no RUN_FINISH after ``seconds`` (by emission ``ts``).

    Deterministic: "now" is the caller-supplied ``now`` (or the latest emission ``ts``);
    the age is ``now - run_start.ts``, never a wall-clock delta.
    """

    kind = "run.stuck"

    def __init__(self, *, seconds: float, response: Response = Response.FLAG) -> None:
        super().__init__(response=response)
        self.seconds = seconds

    def evaluate(
        self, emissions: Sequence[Emission], *, now: float, pipeline: str | None = None
    ) -> Firing | None:
        starts = {e.run_id: e for e in emissions if e.kind is EmissionKind.RUN_START}
        finished = {e.run_id for e in emissions if e.kind is EmissionKind.RUN_FINISH}
        stuck = [
            (run_id, now - e.ts)
            for run_id, e in starts.items()
            if run_id not in finished and (now - e.ts) > self.seconds
        ]
        if not stuck:
            return None
        worst_run, worst_age = max(stuck, key=lambda pair: pair[1])
        return self._fire(
            f"run {worst_run} stuck {worst_age:.0f}s (> {self.seconds:.0f}s)",
            {"run_id": worst_run, "age_s": worst_age},
            now=now,
            pipeline=pipeline,
            emissions=[starts[worst_run]],
        )


class EmissionFloodRule(AnomalyRule):
    """Breach when emission volume in ``window`` reaches ``max_count`` — the loop/flood cap.

    The batch-level runaway kill-switch: a fan-out spinning in a loop emits a flood of
    typed signals; this caps it on count, independent of cost.
    """

    kind = "emission.flood"

    def __init__(
        self, *, max_count: int, window: str = "-1m", response: Response = Response.HALT
    ) -> None:
        super().__init__(response=response)
        self.max_count = max_count
        self.window = window

    def evaluate(
        self, emissions: Sequence[Emission], *, now: float, pipeline: str | None = None
    ) -> Firing | None:
        window = _window_emissions(emissions, self.window, now)
        count = len(window)
        if count < self.max_count:
            return None
        return self._fire(
            f"{count} emissions in {self.window.lstrip('-')} (≥ {self.max_count})",
            {"count": count, "max_count": self.max_count},
            now=now,
            pipeline=pipeline,
            emissions=window,
        )


class BudgetApproachingRule(AnomalyRule):
    """Breach when cumulative MODEL spend reaches ``fraction`` of ``budget_usd``.

    An early-warning before the hard :class:`CostBudget` ceiling — typically a FLAG/ALERT
    that fires while there is still budget left to act on.
    """

    kind = "budget.approaching"

    def __init__(
        self, *, budget_usd: float, fraction: float = 0.8, response: Response = Response.ALERT
    ) -> None:
        super().__init__(response=response)
        if budget_usd <= 0:
            raise ValueError("budget_usd must be > 0")
        self.budget_usd = budget_usd
        self.fraction = fraction

    def evaluate(
        self, emissions: Sequence[Emission], *, now: float, pipeline: str | None = None
    ) -> Firing | None:
        spent = sum(
            _as_float(e.attrs.get("cost_usd")) for e in emissions if e.kind is EmissionKind.MODEL
        )
        ceiling = self.budget_usd * self.fraction
        if spent < ceiling:
            return None
        return self._fire(
            f"${spent:.2f} spent — {spent / self.budget_usd:.0%} of ${self.budget_usd:.2f} budget",
            {"spent_usd": spent, "budget_usd": self.budget_usd, "fraction": self.fraction},
            now=now,
            pipeline=pipeline,
            emissions=emissions,
        )


@dataclass(frozen=True)
class Firing:
    """A rule breach: the originating rule, its response tier, and the finding it emits.

    ``tainted`` records whether any emission in the judged window derived from fluid
    (untrusted) input — surfaced for the dashboard, but it never weakens the decision:
    the breach was computed from typed/structural signals only.
    """

    rule: AnomalyRule
    response: Response
    event: ObserverEvent
    tainted: bool = False

    @property
    def halts(self) -> bool:
        return self.response.halts


class AnomalyEngine:
    """Evaluate a set of :class:`AnomalyRule` over the emission stream and enforce halts.

    :meth:`evaluate` is pure (no side effects) — it returns the firings. :meth:`guard`
    is the orchestrator entry point: it evaluates, persists findings through the
    :class:`~crawfish.observe.ObserverSurface`, and on any HALT firing trips the run's
    :class:`~crawfish.core.context.CancelToken` and forces its
    :class:`~crawfish.core.context.CostBudget` over the ceiling (the cooperative kill).
    """

    def __init__(self, rules: Sequence[AnomalyRule]) -> None:
        self.rules = list(rules)

    def evaluate(
        self,
        emissions: Sequence[Emission],
        *,
        now: float | None = None,
        pipeline: str | None = None,
    ) -> list[Firing]:
        """Run every rule once over ``emissions``; return the firings (no side effects).

        ``now`` defaults to the latest emission ``ts`` (or ``0.0`` for an empty stream),
        keeping evaluation clock-free and deterministic on a seeded stream.
        """
        resolved_now = now if now is not None else _latest_ts(emissions)
        firings: list[Firing] = []
        for rule in self.rules:
            firing = rule.evaluate(emissions, now=resolved_now, pipeline=pipeline)
            if firing is not None:
                firings.append(firing)
        return firings

    def guard(
        self,
        ctx: RunContext,
        emissions: Sequence[Emission],
        *,
        now: float | None = None,
        pipeline: str | None = None,
        surface: ObserverSurface | None = None,
    ) -> list[Firing]:
        """Evaluate, persist findings, and trip the kill-switch on any HALT breach.

        Returns every firing. For each, the :class:`ObserverEvent` is emitted onto the
        observer surface (which also lands a typed OBSERVER emission on the run stream,
        so the breach is itself visible on the dashboard). If *any* firing halts, the
        run's :class:`CancelToken` is cancelled and its :class:`CostBudget` forced over
        its ceiling — both cooperative levers the executor already honours.
        """
        firings = self.evaluate(emissions, now=now, pipeline=pipeline)
        sink = surface or ObserverSurface(ctx.store, org_id=ctx.org_id)
        for firing in firings:
            sink.emit(firing.event)
        if any(f.halts for f in firings):
            self._halt(ctx)
        return firings

    @staticmethod
    def _halt(ctx: RunContext) -> None:
        """Trip both halt levers: cooperative cancel + force the budget over its ceiling.

        ``CancelToken.cancel()`` stops cooperative loops at their next checkpoint;
        forcing the :class:`CostBudget` ceiling below current spend makes the next
        ``charge`` raise :class:`BudgetExceeded`, so a non-cooperative spend path is
        also blocked. Idempotent — calling twice is harmless.
        """
        ctx.cancel_token.cancel()
        budget = ctx.cost_budget
        # Drop the ceiling strictly below current spend so the NEXT charge trips it —
        # even a charge(0.0) after a zero-spend HALT (e.g. an EmissionFlood/StuckRun
        # halt before any model spend). The ceiling intentionally goes negative when
        # spend is ~0 so the non-cooperative lever is truly unconditional.
        budget.limit_usd = budget.spent_usd - _HALT_BUDGET_EPSILON

    @staticmethod
    def enforce_budget(ctx: RunContext, amount_usd: float) -> None:
        """Charge ``amount_usd`` against the run budget, halting on :class:`BudgetExceeded`.

        A convenience for spend paths that want a breach to both raise and trip the
        cancel token, so a cooperative loop checking ``cancel_token`` also stops.
        """
        try:
            ctx.cost_budget.charge(amount_usd)
        except BudgetExceeded:
            ctx.cancel_token.cancel()
            raise


def _latest_ts(emissions: Sequence[Emission]) -> float:
    return max((e.ts for e in emissions), default=0.0)


def _as_float(value: object) -> float:
    return float(value) if isinstance(value, (int, float)) else 0.0


def read_and_guard(
    ctx: RunContext,
    engine: AnomalyEngine,
    *,
    run_id: str | None = None,
    pipeline: str | None = None,
    now: float | None = None,
    store: Store | None = None,
) -> list[Firing]:
    """Read a run's emission stream from the store and :meth:`AnomalyEngine.guard` it.

    The live-tail wiring point the executor calls between iterations: it reads the
    run's typed emissions via :func:`~crawfish.emission.read_emissions` and runs the
    engine, halting the run on a breach. Pure read of the ledger; deterministic given
    a fixed ``now``.
    """
    from crawfish.emission import read_emissions

    target = run_id or ctx.run_id
    src = store or ctx.store
    emissions = read_emissions(src, target, org_id=ctx.org_id)
    return engine.guard(ctx, emissions, now=now, pipeline=pipeline)
