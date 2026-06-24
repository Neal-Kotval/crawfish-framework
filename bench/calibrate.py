"""Calibration probe: does haiku actually slip on the hard task, and is its
self-reported confidence a usable escalation signal? Run before the full benchmark.

    uv run python -m bench.calibrate --model haiku
"""

from __future__ import annotations

import argparse
import asyncio
import json

from bench.hard_task import HARD_TICKETS, hard_baseline_prompt
from bench.transport import parse_stream_json, real_transport


def _parse(text: str) -> dict | None:
    s, e = text.find("{"), text.rfind("}")
    if s == -1 or e <= s:
        return None
    try:
        v = json.loads(text[s : e + 1])
        return v if isinstance(v, dict) else None
    except (ValueError, TypeError):
        return None


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="haiku")
    args = ap.parse_args()
    transport = real_transport()
    cargs = ["--output-format", "stream-json", "--verbose", "--model", args.model]

    correct = 0
    rows = []
    for tk in HARD_TICKETS:
        stdout = await transport(cargs, hard_baseline_prompt(tk.body))
        text, _ = parse_stream_json(stdout)
        v = _parse(text) or {}
        pred = v.get("category")
        conf = v.get("confidence")
        ok = pred == tk.category
        correct += ok
        rows.append((tk.id, tk.category, pred, conf, ok))

    print(f"\n{args.model} on hard task: {correct}/{len(HARD_TICKETS)} correct\n")
    print(f"{'id':<5}{'truth':<16}{'pred':<16}{'conf':<6}{'ok'}")
    for tid, truth, pred, conf, ok in rows:
        print(f"{tid:<5}{truth:<16}{str(pred):<16}{str(conf):<6}{'✓' if ok else '✗'}")
    wrong_confs = [c for _, _, _, c, ok in rows if not ok and isinstance(c, (int, float))]
    right_confs = [c for _, _, _, c, ok in rows if ok and isinstance(c, (int, float))]
    if wrong_confs:
        print(
            f"\nmean confidence — correct: {sum(right_confs) / max(1, len(right_confs)):.2f}  "
            f"wrong: {sum(wrong_confs) / len(wrong_confs):.2f}"
        )


if __name__ == "__main__":
    asyncio.run(main())
