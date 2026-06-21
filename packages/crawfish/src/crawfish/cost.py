"""Cost preview, budgets, and a live spend meter.

Three pieces, all deterministic and explicitly *approximate*:

* :func:`estimate_cost` — a dry-run preview. Given a compiled
  :class:`~crawfish.definition.types.Definition`, an item count, and a
  per-model price table, it predicts dollar spend before a single model call.
  The heuristic is simple on purpose: one "run" per agent per item, priced from
  a flat per-run table keyed by model id. Unpinned agents fall back to
  :data:`~crawfish.runtime.command.DEFAULT_MODEL`'s price. The number is a
  planning aid, not a guarantee.
* :class:`Budget` — a warn/stop policy over spend. It layers on the existing
  hard ceiling (:class:`~crawfish.core.context.CostBudget`) rather than
  replacing it: ``Budget`` decides *ok / warn / stopped*, ``CostBudget``
  hard-kills. A per-day budget reads spend from the :class:`Store` via
  :func:`spent_today`.
* :class:`CostMeter` — a tiny live accumulator that tracks total spend and
  exposes remaining headroom against a :class:`Budget`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from enum import Enum
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from crawfish.core.context import BudgetExceeded, CostBudget
from crawfish.provider import ModelsConfig, resolve_model
from crawfish.runtime.command import DEFAULT_MODEL

if TYPE_CHECKING:
    from crawfish.definition.types import Definition
    from crawfish.store.base import Store

__all__ = [
    "DEFAULT_MODEL_PRICES",
    "CostEstimate",
    "estimate_cost",
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
    """

    model_config = {"frozen": True}

    team_size: int = Field(ge=0)
    items: int = Field(ge=0)
    per_item_usd: float = Field(ge=0.0)
    total_usd: float = Field(ge=0.0)
    per_model: dict[str, float] = Field(default_factory=dict)


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
) -> CostEstimate:
    """Predict the dollar cost of running ``definition`` over ``items`` items.

    Heuristic (deterministic, approximate): charge one run per agent per item,
    priced from ``model_prices`` (defaults to :data:`DEFAULT_MODEL_PRICES`) by
    each agent's resolved model id. Unknown model ids are treated as free so a
    missing price never silently inflates the estimate — pass a fuller table for
    sharper numbers. Pass the project's ``config`` (:class:`ModelsConfig`) so the
    preview resolves aliases + the configured default exactly as the runtime will
    (no second source of truth).
    """
    if items < 0:
        raise ValueError("items must be >= 0")
    prices = model_prices if model_prices is not None else DEFAULT_MODEL_PRICES

    per_model: dict[str, float] = {}
    per_item = 0.0
    for agent in definition.team.agents:
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
