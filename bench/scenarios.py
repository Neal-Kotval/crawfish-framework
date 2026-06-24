"""Deterministic capability demos — the things you can't see in a clean cost table.

These run model-free (or with a controlled fake), so they're free and reproducible.
Each returns a small dict the reporter renders. They isolate framework guarantees that
a hand-rolled loop simply does not have: malformed-output repair, a hard cost ceiling,
transactional idempotency, crash/resume, context-window compaction, and eval-gated
learning.
"""

from __future__ import annotations

import json

from bench.synthetic import PROJECT
from bench.task import build_definition
from crawfish.core.context import CostBudget, RunContext
from crawfish.eval import gate_against_baseline, load_baseline, save_baseline
from crawfish.run import Run, RunStatus
from crawfish.runtime.command import CommandRuntime, Transport
from crawfish.runtime.context_strategy import (
    ConversationTurn,
    ExponentialCompact,
    LinearCompact,
    MaxTokens,
    manage_context,
)
from crawfish.store.sqlite import SqliteStore
from crawfish.validation import ValidationAction


# --------------------------------------------------------------------------- #
# Reliability                                                                    #
# --------------------------------------------------------------------------- #
def _flaky_transport() -> Transport:
    """First call returns malformed output, every call after returns valid JSON.

    Drives the REPAIR policy: the first attempt fails schema validation, the one
    re-prompt recovers. A baseline single-shot call would just return the garbage.
    """
    state = {"n": 0}

    async def spawn(args: list[str], prompt: str) -> str:
        state["n"] += 1
        if state["n"] == 1:
            body = "Hmm, this looks like a bug to me, fairly high severity."
        else:
            body = json.dumps(
                {"category": "bug", "severity": "high", "summary": "Recovered triage."}
            )
        line = json.dumps({"type": "result", "result": body, "total_cost_usd": 0.0, "usage": {}})
        return line + "\n"

    return spawn


async def demo_repair() -> dict:
    """Malformed first reply → REPAIR recovers (crawfish) vs no recovery (baseline)."""
    store = SqliteStore(":memory:")
    runtime = CommandRuntime(transport=_flaky_transport(), default_model="mock")
    definition = build_definition("mock")
    ctx = RunContext(store=store, cost_budget=CostBudget())
    run = Run(
        definition,
        {"project": PROJECT, "ticket_body": "The export button throws a 500 error."},
        runtime=runtime,
        on_invalid=ValidationAction.REPAIR,
    )
    recovered = False
    value = None
    try:
        out = await run.execute(ctx)
        value = out.value
        recovered = isinstance(value, dict) and value.get("category") == "bug"
    except Exception:
        recovered = False
    store.close()
    return {
        "crawfish_recovered": recovered,
        "crawfish_value": value,
        "baseline_recovered": False,  # single-shot garbage, no repair path exists
        "note": "REPAIR re-prompts once with the schema error; the baseline keeps the garbage.",
    }


async def demo_budget_ceiling(transport: Transport, model: str, n: int = 12) -> dict:
    """A hard CostBudget stops the run mid-batch; a hand-rolled loop has no ceiling."""
    from bench.paths import run_crawfish
    from bench.synthetic import tickets

    store = SqliteStore(":memory:")
    items = tickets(n)
    # Set the ceiling below the cost of the full batch so it trips partway through.
    full = await run_crawfish(items, transport=transport, store=store, model=model)
    total_cost = sum(i.cost_usd for i in full.items)
    ceiling = total_cost * 0.4 if total_cost > 0 else 1e-9
    store2 = SqliteStore(":memory:")
    capped = await run_crawfish(
        items, transport=transport, store=store2, model=model, budget_usd=ceiling
    )
    store.close()
    store2.close()
    return {
        "ceiling_usd": round(ceiling, 6),
        "uncapped_items_processed": len(full.items),
        "capped_items_processed": len([i for i in capped.items if i.error is None]),
        "budget_exceeded": capped.budget_exceeded,
        "note": "crawfish hard-kills at the ceiling; the baseline would bill the whole batch.",
    }


def demo_idempotency() -> dict:
    """Transactional claim_idempotency dedupes a re-run; a naive loop reprocesses all."""
    store = SqliteStore(":memory:")
    keys = [f"sink:ticket:{i}" for i in range(10)]
    first_pass = sum(1 for k in keys if store.claim_idempotency(k))
    # Simulate a crash + full re-run: the same keys are presented again.
    second_pass = sum(1 for k in keys if store.claim_idempotency(k))
    store.close()
    return {
        "items": len(keys),
        "first_run_did_work": first_pass,
        "rerun_did_work": second_pass,  # 0 → no duplicate side effects
        "baseline_rerun_would_redo": len(keys),
        "note": "Re-running the batch fires each consequential sink at most once.",
    }


async def demo_resume(transport: Transport, model: str) -> dict:
    """A Run persists to the Store; after a 'crash' it is restored by id."""
    store = SqliteStore(":memory:")
    runtime = CommandRuntime(transport=transport, default_model=model)
    definition = build_definition(model)
    ctx = RunContext(store=store, cost_budget=CostBudget())
    run = Run(
        definition,
        {"project": PROJECT, "ticket_body": "App crashes on launch."},
        runtime=runtime,
    )
    await run.execute(ctx)
    # Simulate process restart: rebuild the Run purely from the Store record.
    restored = Run.restore(store, run.id, definition, runtime=runtime)
    ok = restored.id == run.id and restored.status is RunStatus.DONE
    store.close()
    return {
        "run_id": run.id,
        "persisted_status": run.status.value,
        "restored_status": restored.status.value,
        "resume_ok": ok,
        "baseline_resume": "none — progress is lost on crash",
    }


# --------------------------------------------------------------------------- #
# Context management                                                             #
# --------------------------------------------------------------------------- #
def demo_context() -> dict:
    """Compaction strategies reclaim tokens deterministically; taint propagates."""
    store = SqliteStore(":memory:")
    ctx = RunContext(store=store)
    # A long transcript: 20 turns, every other one carrying untrusted (fluid) data.
    turns = [
        ConversationTurn(
            role="user" if i % 2 == 0 else "assistant",
            text=f"Turn {i}: " + ("ticket payload " * 12),
            is_fluid_data=(i % 2 == 0),
        )
        for i in range(20)
    ]
    before = sum(t.tokens for t in turns)
    results = {}
    for strat in (
        MaxTokens(limit=before // 3, keep_recent=4),
        LinearCompact(threshold=before // 2, keep_recent=6),
        ExponentialCompact(threshold=before // 2, keep_recent=8),
    ):
        managed = manage_context(list(turns), strat, ctx)
        after = sum(t.tokens for t in managed)
        # taint propagation: if any compacted summary turn holds fluid data it stays fluid
        summary_fluid = any(t.role == "system" and t.is_fluid_data for t in managed)
        results[strat.name] = {
            "tokens_before": before,
            "tokens_after": after,
            "reclaimed": before - after,
            "turns_before": len(turns),
            "turns_after": len(managed),
            "summary_stays_tainted": summary_fluid,
        }
    store.close()
    results["note"] = (
        "Deterministic, model-free compaction. A compacted fluid turn stays tainted "
        "(no silent privilege escalation). The baseline has no windowing — it overflows."
    )
    return results


# --------------------------------------------------------------------------- #
# Learning (eval-gated promotion — mechanism only; not auto-wired to live runs)  #
# --------------------------------------------------------------------------- #
def demo_learning_gate() -> dict:
    """The safety property: a regressed candidate is gated; an improved one passes."""
    store = SqliteStore(":memory:")
    name = "bench-classifier"
    save_baseline(store, name, {"accuracy": 0.80, "valid_rate": 1.0})
    worse = gate_against_baseline(store, name, {"accuracy": 0.65, "valid_rate": 1.0})
    better = gate_against_baseline(store, name, {"accuracy": 0.90, "valid_rate": 1.0})
    baseline = load_baseline(store, name)
    store.close()
    return {
        "baseline": baseline,
        "regressed_candidate_promoted": worse,  # False = correctly rejected
        "improved_candidate_promoted": better,  # True = allowed
        "note": (
            "The eval gate (crawfish.eval.gate_against_baseline) blocks a regression from "
            "replacing a working agent. The full LearningLoop.improve() cycle that drives "
            "this from trajectories is built but NOT yet wired to live pipelines (Tuner "
            "knobs unexposed) — so this demonstrates the gate, not autonomous improvement."
        ),
    }
