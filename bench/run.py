"""Benchmark orchestrator: Claude-via-crawfish vs. hand-rolled Claude.

    uv run python -m bench.run --mock                # free, deterministic dry run
    uv run python -m bench.run --live --n 8 --model claude-haiku-4-5   # real claude -p

Writes a Markdown report to bench/RESULTS.md and prints a summary.
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
from pathlib import Path

from bench import scenarios
from bench.paths import PathResult, run_baseline, run_crawfish
from bench.synthetic import tickets
from bench.transport import fake_transport, real_transport
from crawfish.store.sqlite import SqliteStore


def _agg(r: PathResult) -> dict:
    done = [i for i in r.items if i.error is None]
    n = len(done)
    inj = [i for i in r.items if i.injection]
    return {
        "items": len(r.items),
        "errors": len(r.items) - n,
        "total_cost_usd": round(sum(i.cost_usd for i in r.items), 6),
        "input_tokens": sum(i.input_tokens for i in r.items),
        "output_tokens": sum(i.output_tokens for i in r.items),
        "wall_ms": round(r.wall_ms, 1),
        "mean_latency_ms": round(statistics.mean([i.latency_ms for i in r.items]), 1)
        if r.items
        else 0.0,
        "accuracy": round(sum(1 for i in r.items if i.correct) / len(r.items), 3)
        if r.items
        else 0.0,
        "valid_rate": round(sum(1 for i in r.items if i.valid) / len(r.items), 3)
        if r.items
        else 0.0,
        "extra_calls": sum(max(0, i.calls - 1) for i in r.items),
        "injection_items": len(inj),
        "injection_resisted": sum(1 for i in inj if not i.steered),
    }


def _fmt_table(c: dict, b: dict) -> str:
    rows = [
        ("Items processed", c["items"], b["items"]),
        ("Errors / dead-letters", c["errors"], b["errors"]),
        ("Category accuracy", c["accuracy"], b["accuracy"]),
        ("Schema-valid rate", c["valid_rate"], b["valid_rate"]),
        (
            "Injection resisted",
            f"{c['injection_resisted']}/{c['injection_items']}",
            f"{b['injection_resisted']}/{b['injection_items']}",
        ),
        ("Total cost (USD)", c["total_cost_usd"], b["total_cost_usd"]),
        ("Input tokens", c["input_tokens"], b["input_tokens"]),
        ("Output tokens", c["output_tokens"], b["output_tokens"]),
        ("Wall-clock (ms)", c["wall_ms"], b["wall_ms"]),
        ("Mean latency/item (ms)", c["mean_latency_ms"], b["mean_latency_ms"]),
        ("Extra calls (REPAIR)", c["extra_calls"], b["extra_calls"]),
    ]
    out = ["| Metric | Crawfish | Baseline |", "|---|---|---|"]
    for name, cv, bv in rows:
        out.append(f"| {name} | {cv} | {bv} |")
    return "\n".join(out)


async def main_async(args: argparse.Namespace) -> str:
    items = tickets(args.n)
    if args.live:
        transport = real_transport(args.claude_bin)
        mode = "LIVE (`claude -p`)"
    else:
        transport = fake_transport()
        mode = "MOCK (simulated `claude -p`, free & deterministic)"

    store_c = SqliteStore(":memory:")
    crawfish = await run_crawfish(items, transport=transport, store=store_c, model=args.model)
    baseline = await run_baseline(items, transport=transport, model=args.model)
    store_c.close()

    c, b = _agg(crawfish), _agg(baseline)

    # Capability demos (deterministic / free regardless of --live).
    rep = await scenarios.demo_repair()
    bud = await scenarios.demo_budget_ceiling(fake_transport(), args.model, n=min(args.n or 12, 12))
    idem = scenarios.demo_idempotency()
    res = await scenarios.demo_resume(fake_transport(), args.model)
    ctxd = scenarios.demo_context()
    learn = scenarios.demo_learning_gate()

    report = _render(mode, args, c, b, crawfish, baseline, rep, bud, idem, res, ctxd, learn)

    # Console summary
    print(f"\nMode: {mode}   model={args.model}   items={len(items)}")
    print(_fmt_table(c, b))
    print(
        f"\nReliability: repair_recovered={rep['crawfish_recovered']} "
        f"budget_stopped_at={bud['capped_items_processed']}/{bud['uncapped_items_processed']} "
        f"idempotent_rerun_work={idem['rerun_did_work']} resume_ok={res['resume_ok']}"
    )
    print(
        f"Learning gate: regression_blocked={not learn['regressed_candidate_promoted']} "
        f"improvement_allowed={learn['improved_candidate_promoted']}"
    )
    print(f"\nFull report → {args.out}")
    return report


def _render(mode, args, c, b, crawfish, baseline, rep, bud, idem, res, ctxd, learn) -> str:
    L = []
    L.append("# Crawfish vs. hand-rolled Claude — benchmark\n")
    L.append(f"- **Mode:** {mode}")
    L.append(f"- **Model:** `{args.model}`")
    L.append(
        f"- **Workload:** {c['items']} synthetic support tickets with ground-truth labels "
        f"({c['injection_items']} carry prompt-injection payloads)"
    )
    L.append(
        "- **Both paths** run the *same model* over the *same items*, sequentially "
        "(crawfish fan-out is sequential today). The only variable is the framework wrapper.\n"
    )

    if not args.live:
        L.append(
            "> ⚠️ **These are MOCK numbers** from a simulated `claude -p`. Cost/tokens are "
            "synthesized and quality reflects a keyword stub, not the real model. The mock "
            "*does* model the one behavioural delta we expect — the fluid-data boundary "
            "resisting injection — but treat accuracy/cost as illustrative until you run "
            "`--live`. The reliability / context / learning sections are real (model-free).\n"
        )

    L.append("## Bottom line\n")
    L.append(
        "On a clean bulk-classification task with a capable model, **crawfish and a "
        "hand-rolled loop produce near-identical cost, latency, and accuracy.** The "
        "framework's value is not happy-path lift — it's the operational guarantees a loop "
        "lacks: a hard cost ceiling, typed-output validation with automatic repair, "
        "transactional idempotency, crash/resume, and the prompt-injection boundary. Those "
        "are demonstrated (deterministically) in the Reliability section below. You adopt "
        "crawfish for the runs that *don't* go cleanly, not the ones that do.\n"
    )

    L.append("## Headline: cost · latency · quality\n")
    L.append(_fmt_table(c, b))
    L.append("")

    L.append("## What the framework adds (and what it doesn't)\n")
    acc_edge = c["accuracy"] - b["accuracy"]
    inj_edge = c["injection_resisted"] - b["injection_resisted"]
    if acc_edge > 0 or inj_edge > 0:
        quality = (
            "**Quality.** The framework came out ahead here: accuracy "
            f"{c['accuracy']} vs {b['accuracy']}, and the fluid-data fence resisted "
            f"{c['injection_resisted']}/{c['injection_items']} injections vs "
            f"{b['injection_resisted']}/{b['injection_items']} for the inline baseline. "
            "It also enforces a typed schema (`valid_rate`), so malformed replies are caught "
            "and repaired instead of flowing downstream silently."
        )
    else:
        quality = (
            f"**Quality.** No happy-path edge on this run: both paths hit accuracy "
            f"{c['accuracy']}/{b['accuracy']} and resisted "
            f"{c['injection_resisted']}/{c['injection_items']} vs "
            f"{b['injection_resisted']}/{b['injection_items']} injections — a capable model "
            "(haiku) already shrugged off these inline injections without the fence. The "
            "framework's quality value is therefore **insurance, not lift**: the typed "
            "schema + REPAIR catch the malformed/steered reply *when* it happens (see the "
            "deterministic Reliability section), and the boundary is defense-in-depth that "
            "matters more with weaker models, longer data, or stronger attacks. On a clean "
            "task with a strong model, you pay for guarantees you didn't end up needing."
        )
    L.append(quality + "\n")
    L.append(
        "**Cost.** Roughly equal per item, +1 metered call whenever REPAIR fires "
        f"({c['extra_calls']} extra call(s) here). The framework buys correctness at the "
        "cost of an occasional re-prompt, capped by the budget ceiling. Note the absolute "
        f"input-token count ({c['input_tokens']:,} across {c['items']} items) is dominated "
        "by the ~37k-token context the local Claude Code install loads on *every* `claude -p` "
        "call (CLAUDE.md, skills, MCP) — both paths pay it equally, so the comparison holds, "
        "but raw-API numbers would be far lower.\n"
    )
    L.append(
        "**Latency.** ~Equal. Crawfish does **not** win on wall-clock today: fan-out is a "
        "sequential `for` loop (`packages/crawfish/src/crawfish/batch.py:107`), so there is "
        "no parallelism advantage yet. Per-item overhead (validation, ledger writes) is "
        "small relative to a model call. Parallel fan-out is a Phase-2 item.\n"
    )

    L.append("## Reliability (deterministic, model-free)\n")
    L.append(
        f"- **Malformed-output recovery:** crawfish recovered via one REPAIR re-prompt "
        f"→ `{rep['crawfish_value']}`. The hand-rolled baseline has no repair path and keeps the garbage."  # noqa: E501
    )
    L.append(
        f"- **Hard cost ceiling:** with a budget set to ~40% of full-batch cost "
        f"(${bud['ceiling_usd']}), crawfish stopped after {bud['capped_items_processed']} of "
        f"{bud['uncapped_items_processed']} items (`budget_exceeded={bud['budget_exceeded']}`). "
        f"A hand-rolled loop has no ceiling and bills the whole batch."
    )
    L.append(
        f"- **Transactional idempotency:** first run did work on {idem['first_run_did_work']}/"
        f"{idem['items']} items; a full re-run did work on {idem['rerun_did_work']} "
        f"(consequential sinks fire at most once). A naive loop would redo all {idem['baseline_rerun_would_redo']}."  # noqa: E501
    )
    L.append(
        f"- **Crash / resume:** a Run persisted as `{res['persisted_status']}` was rebuilt "
        f"from the Store after a simulated restart (`resume_ok={res['resume_ok']}`). "
        f"Baseline: {res['baseline_resume']}.\n"
    )

    L.append("## Context management (deterministic, model-free)\n")
    L.append(
        "| Strategy | Tokens before | Tokens after | Reclaimed | Turns before→after | Fluid summary stays tainted |"  # noqa: E501
    )
    L.append("|---|---|---|---|---|---|")
    for k, v in ctxd.items():
        if k == "note":
            continue
        L.append(
            f"| {k} | {v['tokens_before']} | {v['tokens_after']} | {v['reclaimed']} | "
            f"{v['turns_before']}→{v['turns_after']} | {v['summary_stays_tainted']} |"
        )
    L.append(f"\n_{ctxd['note']}_\n")

    L.append("## Learning (eval gate — mechanism only)\n")
    L.append(f"- Baseline scores: `{learn['baseline']}`")
    L.append(
        f"- A **regressed** candidate (accuracy 0.65) → promoted: "
        f"`{learn['regressed_candidate_promoted']}` (correctly blocked)."
    )
    L.append(
        f"- An **improved** candidate (accuracy 0.90) → promoted: "
        f"`{learn['improved_candidate_promoted']}` (allowed)."
    )
    L.append(f"\n_{learn['note']}_\n")

    L.append("## Honest caveats\n")
    L.append("1. **Sequential fan-out** — no latency win vs a naive loop yet (Phase 2).")
    L.append(
        "2. **Learning is not autonomous** — the eval *gate* ships and is demonstrated; the "
        "`LearningLoop.improve()` cycle that would drive it from real trajectories is not "
        "wired to live pipelines (Tuner knobs unexposed)."
    )
    L.append(
        "3. **Token accounting** — crawfish's ledger records `cost_usd` but not token "
        "counts; tokens here are parsed by the harness from `claude -p` stream-json."
    )
    L.append(
        "4. **Mock vs live** — run `--live` for real cost/quality. The mock is for "
        "validating the harness and illustrating the boundary effect.\n"
    )
    return "\n".join(L)


def main() -> None:
    p = argparse.ArgumentParser(description="Crawfish vs hand-rolled Claude benchmark")
    p.add_argument("--n", type=int, default=None, help="number of tickets (default: all 18)")
    p.add_argument("--model", default="claude-haiku-4-5", help="model id for both paths")
    p.add_argument("--live", action="store_true", help="use real `claude -p` (costs money)")
    p.add_argument("--mock", action="store_true", help="use simulated transport (default)")
    p.add_argument("--claude-bin", default="claude")
    p.add_argument("--out", default="bench/RESULTS.md")
    args = p.parse_args()
    if args.mock:
        args.live = False
    report = asyncio.run(main_async(args))
    Path(args.out).write_text(report)


if __name__ == "__main__":
    main()
