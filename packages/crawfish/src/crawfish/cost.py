"""Cost preview, budgets, and a live spend meter.

``cost.py`` is the **single owner of the cost model** (Milestone F, F-6 / OPT-2).
Every other plane that needs a dollar number — ``CL-3`` (cost-aware refine
preflight), ``ALG-5`` (cost-regularized objective), the ``craw dev --estimate``
surface — is a *consumer* of the API here and MUST NOT re-implement estimation or
re-define the composition law. There is exactly one place where an operator's
cost multiplier lives, and it is :class:`CostShape` below.

Pieces, all deterministic and explicitly *approximate* (no live model call ever
happens inside this module — it is a pure function of its inputs):

* :func:`estimate_cost` — a dry-run preview. Given a compiled
  :class:`~crawfish.definition.types.Definition`, an item count, and a
  per-model price table, it predicts dollar spend before a single model call.
  The heuristic is simple on purpose: one "run" per agent per item, priced from
  a flat per-run table keyed by model id. Unpinned agents fall back to
  :data:`~crawfish.runtime.command.DEFAULT_MODEL`'s price. The scalar
  ``total_usd`` is the **lower bound** — it assumes every cost-bearing operator
  fires exactly once.
* :class:`CostShape` + :func:`compose_cost` — the operator-cost layer that the
  audit's Gaps #5/#9 demand. A :class:`Definition`'s lower bound is blind to the
  re-run multipliers of ``Refine`` (``max_iters``), ``Escalate`` (``2×``, on the
  strong model), ``Quorum`` (``k``), ``Retry`` (``n``) and ``recurse``
  (``b^max_depth``). :class:`CostShape` names one such wrapper; the
  **composition law is multiplicative along operator nesting**:
  ``worst_case = lower_bound × Π(per-operator multiplier)``. :func:`compose_cost`
  folds a nesting of shapes (outermost first) onto a base :class:`CostEstimate`,
  producing the additive ``expected_usd`` / ``worst_case_usd`` fields without
  ever touching ``total_usd``.
* :class:`Budget` — a warn/stop policy over spend. It layers on the existing
  hard ceiling (:class:`~crawfish.core.context.CostBudget`) rather than
  replacing it: ``Budget`` decides *ok / warn / stopped*, ``CostBudget``
  hard-kills. A per-day budget reads spend from the :class:`Store` via
  :func:`spent_today`.
* :class:`CostMeter` — a tiny live accumulator that tracks total spend and
  exposes remaining headroom against a :class:`Budget`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from enum import Enum
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field, model_validator

from crawfish.core.context import BudgetExceeded, CostBudget
from crawfish.provider import ModelsConfig, resolve_model
from crawfish.routing import RoutingPolicy, route_decision
from crawfish.runtime.command import DEFAULT_MODEL

if TYPE_CHECKING:
    from crawfish.definition.types import Definition
    from crawfish.store.base import Store

__all__ = [
    "DEFAULT_MODEL_PRICES",
    "CostEstimate",
    "estimate_cost",
    "CostShape",
    "compose_cost",
    "BudgetState",
    "Budget",
    "spent_today",
    "CostMeter",
    "BudgetExceeded",
    "CostBudget",
]

# Flat, approximate USD-per-run prices keyed by model id. A "run" is one agent
# turn on one item. These are deliberately coarse planning numbers, not billing
# truth; override via the ``model_prices`` argument for sharper estimates. The
# ``mock`` model is free so test/replay pipelines preview at $0.
DEFAULT_MODEL_PRICES: dict[str, float] = {
    "claude-opus-4-8": 0.30,
    "claude-sonnet-4-6": 0.06,
    "claude-haiku-4-5": 0.01,
    "mock": 0.0,
}


class CostEstimate(BaseModel):
    """A dry-run cost preview for a Definition.

    All figures are USD and approximate. ``per_item_usd`` is the predicted spend
    for a single item across the whole team; ``total_usd`` scales that by the
    item count. ``per_model`` breaks the total down by resolved model id so a
    caller can see which model dominates the bill.

    The estimate is a **three-number interval** (F-6 / OPT-2):

    * ``total_usd`` — the **lower bound** (unchanged semantics): every
      cost-bearing operator fires exactly once. This field's meaning is frozen;
      consumers and existing callers may rely on it.
    * ``worst_case_usd`` — the lower bound times the product of every operator's
      worst-case multiplier (see :class:`CostShape` / :func:`compose_cost`). With
      no operator wrappers it equals ``total_usd``.
    * ``expected_usd`` — a *measured-rate* band between the two. When no measured
      rates are supplied it equals ``worst_case_usd`` (never undercount).
      ``expected_lo_usd`` / ``expected_hi_usd`` carry the CI so the number is a
      band, never a falsely-precise point.

    Invariant (enforced): ``total_usd <= expected_lo_usd <= expected_usd <=
    expected_hi_usd <= worst_case_usd``.
    """

    model_config = {"frozen": True}

    team_size: int = Field(ge=0)
    items: int = Field(ge=0)
    per_item_usd: float = Field(ge=0.0)
    total_usd: float = Field(ge=0.0)
    per_model: dict[str, float] = Field(default_factory=dict)
    # Additive operator-aware fields (F-6). Default to the lower bound so a bare
    # `CostEstimate(...)` (the old construction site / no operator wrappers) is a
    # degenerate interval `[total_usd, total_usd, total_usd]` — back-compatible.
    expected_usd: float = Field(default=-1.0)
    worst_case_usd: float = Field(default=-1.0)
    expected_lo_usd: float = Field(default=-1.0)
    expected_hi_usd: float = Field(default=-1.0)

    @model_validator(mode="after")
    def _default_interval(self) -> CostEstimate:
        """Fill the additive fields from ``total_usd`` when not supplied, then
        assert the interval invariant.

        A sentinel ``-1.0`` means "not supplied" — collapse it to ``total_usd``
        so legacy callers (and any code constructing a bare estimate) get a
        well-formed degenerate interval. With the fields present we still verify
        the ordering so a malformed interval can never be minted.
        """
        # `frozen=True` blocks normal assignment; mutate via object.__setattr__,
        # which pydantic itself uses internally for validated fields.
        if self.worst_case_usd < 0.0:
            object.__setattr__(self, "worst_case_usd", self.total_usd)
        if self.expected_usd < 0.0:
            object.__setattr__(self, "expected_usd", self.worst_case_usd)
        if self.expected_lo_usd < 0.0:
            object.__setattr__(self, "expected_lo_usd", self.expected_usd)
        if self.expected_hi_usd < 0.0:
            object.__setattr__(self, "expected_hi_usd", self.expected_usd)
        # Lower bound never exceeds the band; band never exceeds worst case.
        eps = 1e-9
        if not (
            self.total_usd <= self.expected_lo_usd + eps
            and self.expected_lo_usd <= self.expected_usd + eps
            and self.expected_usd <= self.expected_hi_usd + eps
            and self.expected_hi_usd <= self.worst_case_usd + eps
        ):
            raise ValueError(
                "cost interval must satisfy "
                "total_usd <= expected_lo <= expected <= expected_hi <= worst_case "
                f"(got total={self.total_usd}, lo={self.expected_lo_usd}, "
                f"expected={self.expected_usd}, hi={self.expected_hi_usd}, "
                f"worst_case={self.worst_case_usd})"
            )
        return self


def _resolve_model(model: str | list[str] | None, config: ModelsConfig | None = None) -> str:
    """Resolve an agent's ``model`` field to a single id (delegates to the shared
    resolver so the estimate can never drift from what the runtime actually runs).

    The same ``config`` the runtime uses supplies named aliases + the configured
    project default; unpinned (``None``) agents fall back to :data:`DEFAULT_MODEL`
    only when no ``config.default`` is set. See :func:`crawfish.provider.resolve_model`.
    """
    return resolve_model(model, default=DEFAULT_MODEL, config=config)


def estimate_cost(
    definition: Definition,
    *,
    items: int = 1,
    model_prices: dict[str, float] | None = None,
    config: ModelsConfig | None = None,
    routing: RoutingPolicy | None = None,
) -> CostEstimate:
    """Predict the dollar cost of running ``definition`` over ``items`` items.

    Heuristic (deterministic, approximate): charge one run per agent per item,
    priced from ``model_prices`` (defaults to :data:`DEFAULT_MODEL_PRICES`) by
    each agent's resolved model id. Unknown model ids are treated as free so a
    missing price never silently inflates the estimate — pass a fuller table for
    sharper numbers. Pass the project's ``config`` (:class:`ModelsConfig`) so the
    preview resolves aliases + the configured default exactly as the runtime will
    (no second source of truth).

    When a :class:`~crawfish.routing.RoutingPolicy` is supplied (CRA-182 smart
    routing), each agent's model is resolved through the **same**
    :func:`crawfish.routing.route_decision` the runtime
    (:class:`~crawfish.runtime.routing_runtime.RoutingRuntime`) uses — which in turn
    calls the single shared :func:`crawfish.provider.resolve_model`. So a routed step
    (e.g. a cheap step sent to ``"local"``) is previewed at exactly the model that will
    run: the estimate cannot drift from the routed run (CRA-186).
    """
    if items < 0:
        raise ValueError("items must be >= 0")
    prices = model_prices if model_prices is not None else DEFAULT_MODEL_PRICES

    per_model: dict[str, float] = {}
    per_item = 0.0
    for agent in definition.team.agents:
        if routing is not None:
            # Route through the shared decision point so preview == run (no second path).
            model = route_decision(
                definition, agent.role, policy=routing, default=DEFAULT_MODEL, config=config
            ).resolved
        else:
            model = _resolve_model(agent.model, config)
        price = prices.get(model, 0.0)
        per_item += price
        per_model[model] = per_model.get(model, 0.0) + price * items

    return CostEstimate(
        team_size=len(definition.team.agents),
        items=items,
        per_item_usd=per_item,
        total_usd=per_item * items,
        per_model=per_model,
    )


@dataclass(frozen=True)
class CostShape:
    """One cost-bearing operator wrapper and its re-run multiplier (F-6 / OPT-2).

    A bare :class:`Definition` estimate assumes each agent runs once; the control
    plane wraps that base call in operators that re-run the leaf. :class:`CostShape`
    names one such wrapper. The **worst-case multiplier** is the most times the
    inner call can fire:

    ====================  ======================  ==================================
    Operator              ``kind``                worst-case multiplier
    ====================  ======================  ==================================
    ``Refine``            ``"refine"``            ``max_iters``
    ``Escalate``          ``"escalate"``          ``2`` (2nd attempt on the strong
                                                  model — see ``strong_multiplier``)
    ``Quorum``            ``"quorum"``            ``k``
    ``Retry``             ``"retry"``             ``n``
    ``recurse``           ``"recurse"``           ``b ** max_depth``
    ====================  ======================  ==================================

    Use the classmethod constructors (:meth:`refine`, :meth:`escalate`,
    :meth:`quorum`, :meth:`retry`, :meth:`recurse`) rather than the raw fields —
    they encode each operator's multiplier law in exactly one place.

    ``measured_rate`` (optional, in ``[0, 1]``) is the *measured fraction of calls
    that actually trigger the extra work* — e.g. an escalation rate of 0.2 means
    20% of calls escalate to the strong model. It comes from ``cw.calibrate`` or
    the ledger and is used by :func:`compose_cost` to build the **expected** band.
    With no rate the operator is priced at its worst case (never undercount).
    ``rate_ci`` is the half-width of the rate's confidence interval (also in
    ``[0, 1]``); it widens the expected band so the number is never falsely precise.

    ``strong_multiplier`` (escalation only) re-prices the escalated attempt: the
    second attempt runs on the *strong* model, so its marginal cost is
    ``strong_price / base_price`` rather than ``1``. :meth:`escalate` computes it
    from the two per-call prices.
    """

    kind: str
    worst_case_multiplier: float
    measured_rate: float | None = None
    rate_ci: float = 0.0
    # For escalation: marginal cost of the extra (strong-model) attempt, as a
    # multiple of one base call. 1.0 means "same price as base". For all other
    # operators the extra calls are priced at the base rate, so this stays 1.0.
    strong_multiplier: float = 1.0

    def __post_init__(self) -> None:
        if self.worst_case_multiplier < 1.0:
            raise ValueError(f"{self.kind}: worst_case_multiplier must be >= 1.0")
        if self.measured_rate is not None and not 0.0 <= self.measured_rate <= 1.0:
            raise ValueError(f"{self.kind}: measured_rate must be in [0, 1]")
        if not 0.0 <= self.rate_ci <= 1.0:
            raise ValueError(f"{self.kind}: rate_ci must be in [0, 1]")
        if self.strong_multiplier < 0.0:
            raise ValueError(f"{self.kind}: strong_multiplier must be >= 0.0")

    # --- worst-case factor -------------------------------------------------
    def worst_case_factor(self) -> float:
        """The multiplier this operator contributes to ``worst_case``.

        For escalation the second attempt is re-priced on the strong model:
        ``1 + strong_multiplier`` (one base call + one strong call). For every
        other operator the inner call simply fires ``worst_case_multiplier``
        times at the base rate.
        """
        if self.kind == "escalate":
            return 1.0 + self.strong_multiplier
        return self.worst_case_multiplier

    # --- expected factor (measured-rate band) ------------------------------
    def expected_factor(self, *, ci_sign: float = 0.0) -> float:
        """The multiplier this operator contributes to the **expected** band.

        With no ``measured_rate`` this is the worst-case factor (never
        undercount). With a rate ``p`` the operator fires its extra work only a
        ``p`` fraction of the time, so the expected factor interpolates between
        "always once" (``p=0``) and the worst case (``p=1``):

            ``expected = 1 + p · (worst_case_factor − 1)``

        ``ci_sign`` in ``{-1, 0, +1}`` shifts ``p`` by ``rate_ci`` to build the
        lo / point / hi edges of the band (clamped to ``[0, 1]``).
        """
        if self.measured_rate is None:
            return self.worst_case_factor()
        p = self.measured_rate + ci_sign * self.rate_ci
        p = min(1.0, max(0.0, p))
        return 1.0 + p * (self.worst_case_factor() - 1.0)

    # --- ergonomic constructors (the multiplier law, one place each) -------
    @classmethod
    def refine(
        cls, max_iters: int, *, measured_rate: float | None = None, rate_ci: float = 0.0
    ) -> CostShape:
        """``Refine`` worst case = ``max_iters`` inner runs."""
        if max_iters < 1:
            raise ValueError("refine max_iters must be >= 1")
        return cls("refine", float(max_iters), measured_rate=measured_rate, rate_ci=rate_ci)

    @classmethod
    def escalate(
        cls,
        *,
        base_price: float,
        strong_price: float,
        measured_rate: float | None = None,
        rate_ci: float = 0.0,
    ) -> CostShape:
        """``Escalate`` worst case = base call + one strong-model attempt.

        The 2× multiplier from the spec is the *count*; the *cost* re-prices the
        second attempt on ``strong_price`` (escalation re-priced on the strong
        model, F-6). ``strong_multiplier = strong_price / base_price``.
        """
        if base_price <= 0.0:
            # A free/zero base call has no meaningful strong ratio; fall back to
            # the count-based 2× so worst_case is still defined.
            strong_mult = 1.0
        else:
            strong_mult = strong_price / base_price
        return cls(
            "escalate",
            2.0,
            measured_rate=measured_rate,
            rate_ci=rate_ci,
            strong_multiplier=strong_mult,
        )

    @classmethod
    def quorum(cls, k: int) -> CostShape:
        """``Quorum`` worst case = ``k`` samples (always fires — no rate)."""
        if k < 1:
            raise ValueError("quorum k must be >= 1")
        return cls("quorum", float(k))

    @classmethod
    def retry(
        cls, n: int, *, measured_rate: float | None = None, rate_ci: float = 0.0
    ) -> CostShape:
        """``Retry`` worst case = ``n`` attempts."""
        if n < 1:
            raise ValueError("retry n must be >= 1")
        return cls("retry", float(n), measured_rate=measured_rate, rate_ci=rate_ci)

    @classmethod
    def recurse(cls, *, branching: int, max_depth: int) -> CostShape:
        """``recurse`` worst case = ``branching ** max_depth`` leaf calls."""
        if branching < 1:
            raise ValueError("recurse branching must be >= 1")
        if max_depth < 0:
            raise ValueError("recurse max_depth must be >= 0")
        return cls("recurse", float(branching**max_depth))


def compose_cost(base: CostEstimate, shapes: Sequence[CostShape]) -> CostEstimate:
    """Fold a nesting of :class:`CostShape`s onto a base estimate (F-6 / OPT-2).

    ``shapes`` is the operator nesting **outermost-first** (e.g.
    ``[refine(3), quorum(5)]`` for ``Refine(max_iters=3)`` wrapping
    ``Quorum(k=5)``). The composition law is **multiplicative along the
    nesting**::

        worst_case = base.total_usd × Π shape.worst_case_factor()
        expected   = base.total_usd × Π shape.expected_factor()   (measured-rate band)

    ``total_usd`` is carried through untouched — it remains the lower bound. The
    returned estimate's ``expected_lo_usd`` / ``expected_hi_usd`` fold the
    per-operator ``rate_ci`` so ``expected`` is a band, never a point. With no
    shapes (or no measured rates) ``expected == worst_case`` — the estimator
    never undercounts.

    Pure function of its inputs: no model call, no ledger read, no mutation. The
    returned :class:`CostEstimate` is a fresh frozen value.
    """
    lower = base.total_usd
    worst_factor = 1.0
    exp_factor = 1.0
    lo_factor = 1.0
    hi_factor = 1.0
    for shape in shapes:
        worst_factor *= shape.worst_case_factor()
        exp_factor *= shape.expected_factor(ci_sign=0.0)
        lo_factor *= shape.expected_factor(ci_sign=-1.0)
        hi_factor *= shape.expected_factor(ci_sign=+1.0)

    worst = lower * worst_factor
    expected = lower * exp_factor
    lo = lower * lo_factor
    hi = lower * hi_factor
    # Clamp the band into [lower, worst] to absorb any float drift and guarantee
    # the model invariant (and: expected >= lower, expected <= worst).
    lo = min(max(lo, lower), worst)
    hi = min(max(hi, lo), worst)
    expected = min(max(expected, lo), hi)

    return base.model_copy(
        update={
            "worst_case_usd": worst,
            "expected_usd": expected,
            "expected_lo_usd": lo,
            "expected_hi_usd": hi,
        }
    )


class BudgetState(str, Enum):
    """Where spend sits relative to a :class:`Budget`'s thresholds."""

    OK = "ok"  # below the warn threshold
    WARN = "warn"  # at/over warn, still below stop
    STOPPED = "stopped"  # at/over the hard stop


@dataclass
class Budget:
    """A warn/stop spend policy.

    ``stop_usd`` is the hard ceiling; ``warn_usd`` (default 80% of stop) is the
    soft line where callers should surface a warning. ``None`` for ``stop_usd``
    means unbounded — every check is :attr:`BudgetState.OK`. Use :meth:`check`
    for the soft signal and :meth:`as_cost_budget` to hand the orchestrator the
    matching hard ceiling.
    """

    stop_usd: float | None = None
    warn_usd: float | None = None

    def __post_init__(self) -> None:
        if self.warn_usd is None and self.stop_usd is not None:
            self.warn_usd = self.stop_usd * 0.8
        if (
            self.warn_usd is not None
            and self.stop_usd is not None
            and self.warn_usd > self.stop_usd
        ):
            raise ValueError("warn_usd must be <= stop_usd")

    def check(self, spent_usd: float) -> BudgetState:
        """Classify ``spent_usd`` as ok / warn / stopped."""
        if self.stop_usd is not None and spent_usd >= self.stop_usd:
            return BudgetState.STOPPED
        if self.warn_usd is not None and spent_usd >= self.warn_usd:
            return BudgetState.WARN
        return BudgetState.OK

    def as_cost_budget(self, *, spent_usd: float = 0.0) -> CostBudget:
        """Project the hard ceiling onto a :class:`CostBudget` for the runtime."""
        return CostBudget(limit_usd=self.stop_usd, spent_usd=spent_usd)


def _event_cost(event: dict[str, object]) -> float:
    """Pull a USD cost off a telemetry event, tolerating shape drift.

    Looks both at the top level (legacy loose dicts) and under ``attrs`` (the typed
    :class:`~crawfish.emission.Emission` envelope, which nests the cost there).
    """
    sources: list[dict[str, object]] = [event]
    attrs = event.get("attrs")
    if isinstance(attrs, dict):
        sources.append(attrs)
    for source in sources:
        for key in ("cost_usd", "total_cost_usd", "cost"):
            value = source.get(key)
            if isinstance(value, (int, float)):
                return float(value)
    return 0.0


# Telemetry kinds that carry a run's model spend: the typed Emission kinds
# (``model`` per turn, ``run_finish`` per run) plus the legacy loose-dict kinds
# (``runtime.run`` / ``run.finish``) so old ledgers still total correctly.
_COST_BEARING_KINDS = ("model", "run_finish", "runtime.run", "run.finish")


def _parse_event_ts(ts: object) -> datetime | None:
    """Parse a telemetry timestamp to a UTC datetime, or None if not usable.

    Accepts an ISO-8601 string (legacy loose dicts) or an epoch-seconds float (the
    typed :class:`~crawfish.emission.Emission` envelope). A zero/negative epoch
    (the unstamped default) or an unparseable value returns None so the caller
    counts the event rather than silently dropping it.
    """
    if isinstance(ts, str):
        try:
            parsed = datetime.fromisoformat(ts)
        except ValueError:
            return None
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
    if isinstance(ts, (int, float)) and not isinstance(ts, bool) and ts > 0:
        return datetime.fromtimestamp(float(ts), UTC)
    return None


def spent_today(
    store: Store,
    *,
    org_id: str = "local",
    run_ids: list[str] | None = None,
    today: date | None = None,
    now: datetime | None = None,
) -> float:
    """Sum today's spend from the Store's run telemetry (UTC day).

    Reads ``runtime.run`` / ``run.finish`` events that carry a cost field and a
    ``ts`` timestamp, keeping only those dated to ``today`` (defaults to the
    current UTC date). ``run_ids`` narrows the scan; if omitted, the caller is
    responsible for passing the runs to total (the Store seam is per-run, so
    there is no cheap cross-run scan). Events without a usable timestamp are
    counted, so a meter never silently undercounts.
    """
    if today is None:
        today = (now or datetime.now(UTC)).astimezone(UTC).date()
    if run_ids is None:
        return 0.0

    total = 0.0
    for run_id in run_ids:
        for event in store.events(run_id, org_id=org_id):
            if event.get("kind") not in _COST_BEARING_KINDS:
                continue
            parsed = _parse_event_ts(event.get("ts"))
            # A usable timestamp on another day is excluded; an unparseable/zero ts
            # is counted (never silently undercount). Typed emissions carry an epoch
            # float ``ts``; legacy loose dicts carry an ISO-8601 string.
            if parsed is not None and parsed.date() != today:
                continue
            total += _event_cost(event)
    return total


@dataclass
class CostMeter:
    """A live spend accumulator checked against a :class:`Budget`.

    Call :meth:`charge` as runs complete; :attr:`total_usd` is running spend,
    :attr:`remaining_usd` is headroom to the hard stop, and :meth:`state`
    reports the current :class:`BudgetState`.
    """

    budget: Budget = field(default_factory=Budget)
    total_usd: float = 0.0

    def charge(self, amount_usd: float) -> BudgetState:
        """Add ``amount_usd`` to running spend and return the resulting state."""
        if amount_usd < 0:
            raise ValueError("amount_usd must be >= 0")
        self.total_usd += amount_usd
        return self.state()

    def state(self) -> BudgetState:
        return self.budget.check(self.total_usd)

    @property
    def remaining_usd(self) -> float | None:
        """Headroom to the hard stop, or ``None`` if the budget is unbounded."""
        if self.budget.stop_usd is None:
            return None
        return max(0.0, self.budget.stop_usd - self.total_usd)
