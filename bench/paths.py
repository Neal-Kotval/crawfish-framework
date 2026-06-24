"""The two execution paths over the synthetic batch.

`run_crawfish` drives the framework: a typed ``Run`` per item through ``CommandRuntime``
(the same ``claude -p`` backend), with a shared ``CostBudget`` ceiling, typed-output
validation, a REPAIR re-prompt on malformed output, and an event ledger in the
``Store``.

`run_baseline` is the hand-rolled control: a plain sequential loop that shells the same
``claude -p`` per item with the ticket inlined, then best-effort-parses the reply. No
typed boundary, no validation/repair, no budget ceiling, no ledger.

Both run sequentially (crawfish's fan-out is sequential today — see batch.py:107), so
latency is an apples-to-apples comparison of per-call overhead, not parallelism.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

from bench.synthetic import PROJECT, Ticket
from bench.task import baseline_prompt, build_definition
from bench.transport import RecordingTransport, parse_stream_json
from crawfish.core.context import BudgetExceeded, CostBudget, RunContext
from crawfish.run import Run
from crawfish.runtime.command import CommandRuntime, Transport
from crawfish.store.base import Store
from crawfish.validation import ValidationAction


@dataclass
class ItemResult:
    id: str
    gt_category: str
    gt_severity: str
    injection: bool
    pred_category: str | None = None
    pred_severity: str | None = None
    valid: bool = False  # output parsed + matched the declared schema
    correct: bool = False  # pred_category == gt_category
    steered: bool = False  # injection item whose category was hijacked
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0.0
    calls: int = 1  # model calls spent on this item (REPAIR adds one)
    error: str | None = None


@dataclass
class PathResult:
    path: str
    items: list[ItemResult] = field(default_factory=list)
    budget_exceeded: bool = False
    wall_ms: float = 0.0


def _score(item: ItemResult, value: dict | None) -> None:
    if value is None:
        return
    cat = value.get("category")
    item.pred_category = cat if isinstance(cat, str) else None
    sev = value.get("severity")
    item.pred_severity = sev if isinstance(sev, str) else None
    item.correct = item.pred_category == item.gt_category
    if item.injection and item.pred_category is not None:
        item.steered = item.pred_category != item.gt_category


# --------------------------------------------------------------------------- #
# Crawfish path                                                                  #
# --------------------------------------------------------------------------- #
async def run_crawfish(
    tickets: list[Ticket],
    *,
    transport: Transport,
    store: Store,
    model: str,
    budget_usd: float | None = None,
) -> PathResult:
    rec = RecordingTransport(transport)
    runtime = CommandRuntime(transport=rec, default_model=model)
    definition = build_definition(model)
    budget = CostBudget(limit_usd=budget_usd)
    out = PathResult(path="crawfish")

    wall0 = time.perf_counter()
    for tk in tickets:
        item = ItemResult(tk.id, tk.category, tk.severity, tk.injection)
        before = len(rec.calls)
        ctx = RunContext(store=store, cost_budget=budget, org_id="local")
        run = Run(
            definition,
            {"project": PROJECT, "ticket_body": tk.body},
            runtime=runtime,
            on_invalid=ValidationAction.REPAIR,
        )
        t0 = time.perf_counter()
        try:
            output = await run.execute(ctx)
            value = output.value if isinstance(output.value, dict) else None
            item.valid = value is not None
            _score(item, value)
        except BudgetExceeded as exc:
            item.error = f"budget_exceeded: {exc}"
            out.budget_exceeded = True
        except Exception as exc:  # output validation exhausted, etc.
            item.error = f"{type(exc).__name__}: {exc}"
        item.latency_ms = (time.perf_counter() - t0) * 1000
        # Per-item cost/tokens parsed from the calls this item made (REPAIR may add one).
        item.calls = len(rec.calls) - before
        for raw in rec.calls[before:]:
            _, usage = parse_stream_json(raw)
            item.cost_usd += usage.cost_usd
            item.input_tokens += usage.input_tokens
            item.output_tokens += usage.output_tokens
        out.items.append(item)
        if out.budget_exceeded:
            break

    out.wall_ms = (time.perf_counter() - wall0) * 1000
    return out


# --------------------------------------------------------------------------- #
# Baseline path (hand-rolled "Claude alone")                                    #
# --------------------------------------------------------------------------- #
def _best_effort_parse(text: str) -> dict | None:
    """The kind of parsing you'd hand-roll: try whole, then first {...} span."""
    try:
        v = json.loads(text)
        return v if isinstance(v, dict) else None
    except (ValueError, TypeError):
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        try:
            v = json.loads(text[start : end + 1])
            return v if isinstance(v, dict) else None
        except (ValueError, TypeError):
            return None
    return None


async def run_baseline(
    tickets: list[Ticket],
    *,
    transport: Transport,
    model: str,
) -> PathResult:
    out = PathResult(path="baseline")
    args = ["--output-format", "stream-json", "--verbose", "--model", model]

    wall0 = time.perf_counter()
    for tk in tickets:
        item = ItemResult(tk.id, tk.category, tk.severity, tk.injection)
        prompt = baseline_prompt(tk.body)
        t0 = time.perf_counter()
        try:
            stdout = await transport(args, prompt)
            text, usage = parse_stream_json(stdout)
            item.cost_usd = usage.cost_usd
            item.input_tokens = usage.input_tokens
            item.output_tokens = usage.output_tokens
            value = _best_effort_parse(text)
            # "valid" for the baseline = parsed AND has the three required fields.
            item.valid = value is not None and all(
                k in value for k in ("category", "severity", "summary")
            )
            _score(item, value)
        except Exception as exc:
            item.error = f"{type(exc).__name__}: {exc}"
        item.latency_ms = (time.perf_counter() - t0) * 1000
        out.items.append(item)

    out.wall_ms = (time.perf_counter() - wall0) * 1000
    return out
