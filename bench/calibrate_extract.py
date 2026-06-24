"""Probe: does the cheap model botch PO totals, and does the inconsistency signal catch
it? Run before the full extraction benchmark.

    uv run python -m bench.calibrate_extract --model haiku
"""

from __future__ import annotations

import argparse
import asyncio

from bench.extract_task import (
    extract_baseline_prompt,
    inconsistent_total,
    parse_order,
    pos,
    total_correct,
)
from bench.transport import parse_stream_json, real_transport
from crawfish.runtime.base import RunResult


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="haiku")
    args = ap.parse_args()
    transport = real_transport()
    cargs = ["--output-format", "stream-json", "--verbose", "--model", args.model]

    correct = 0
    flagged_wrong = 0
    wrong = 0
    print(f"\n{'id':<5}{'true':>11}{'pred':>13}  {'ok':<4}{'self-consistent?':<18}")
    for p in pos():
        stdout = await transport(cargs, extract_baseline_prompt(p.text))
        text, _ = parse_stream_json(stdout)
        order = parse_order(text) or {}
        ok = total_correct(order, p)
        inconsistent = inconsistent_total(RunResult(text=text))
        correct += ok
        if not ok:
            wrong += 1
            flagged_wrong += 1 if inconsistent else 0
        pred = order.get("grand_total")
        pred_n = pred if isinstance(pred, (int, float)) else -1
        flag = "inconsistent" if inconsistent else "consistent"
        print(f"{p.id:<5}{p.true_total:>11.2f}{pred_n:>13.2f}  {'✓' if ok else '✗':<4}{flag:<18}")
    print(f"\n{args.model}: {correct}/{len(pos())} totals correct")
    if wrong:
        print(
            f"of {wrong} wrong answers, {flagged_wrong} were flagged inconsistent (would escalate)"
        )


if __name__ == "__main__":
    asyncio.run(main())
