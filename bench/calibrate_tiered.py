"""Probe both tiers on the tiered task — does a quality gap finally exist, and does the
chain-consistency signal flag the cheap model's errors?

    uv run python -m bench.calibrate_tiered
"""

from __future__ import annotations

import asyncio

from bench.tiered_task import (
    chain_inconsistent,
    parse_order,
    tiered_baseline_prompt,
    tiered_correct,
    tiered_pos,
    true_grand_total,
)
from bench.transport import parse_stream_json, real_transport
from crawfish.runtime.base import RunResult


async def probe(model: str) -> None:
    transport = real_transport()
    cargs = ["--output-format", "stream-json", "--verbose", "--model", model]
    correct = wrong = flagged = 0
    print(f"\n=== {model} ===")
    print(f"{'id':<5}{'true':>11}{'pred':>12}  ok   flag")
    for p in tiered_pos():
        stdout = await transport(cargs, tiered_baseline_prompt(p.text))
        text, _ = parse_stream_json(stdout)
        order = parse_order(text) or {}
        ok = tiered_correct(order, p)
        inconsistent = chain_inconsistent(RunResult(text=text))
        correct += ok
        if not ok:
            wrong += 1
            flagged += 1 if inconsistent else 0
        pred = order.get("grand_total")
        pred_n = pred if isinstance(pred, (int, float)) else -1
        print(
            f"{p.id:<5}{true_grand_total(p):>11.2f}{pred_n:>12.2f}  "
            f"{'✓' if ok else '✗'}    {'esc' if inconsistent else '-'}"
        )
    print(
        f"{model}: {correct}/{len(tiered_pos())} correct; "
        f"{flagged}/{wrong} wrong answers would escalate"
    )


async def main() -> None:
    await probe("haiku")
    await probe("sonnet")


if __name__ == "__main__":
    asyncio.run(main())
