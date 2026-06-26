# Authoring benchmark — pilot results

Live `claude -p --output-format json`, **n = 1 per cell** (illustrative; run with `CRAW_BENCH_K≥5`
for intervals). Analysis + features: [`docs/dev/craw-code/03-BENCHMARKS.md`](../../docs/dev/craw-code/03-BENCHMARKS.md).

## Speed / cost (measured)

| Cell | Cost | Wall-clock | Turns | In tok | Out tok |
| --- | --- | --- | --- | --- | --- |
| base · T1 classifier | $0.471 | 36.6 s | 6 | 18,734 | 1,872 |
| craw · T1 classifier | $0.495 | 31.6 s | 6 | 18,732 | 1,796 |
| base · T2 sink-safety | $0.545 | 47.3 s | 8 | 18,463 | 2,360 |
| craw · T2 sink-safety | $0.692 | 84.9 s | 10 | 18,867 | 4,843 |

## Quality (authored output through the real craw pipeline)

| Cell | Compiles | Gate-clean | Sink-target-safe | Output flows |
| --- | --- | --- | --- | --- |
| base · T1 | ✅ | ❌ reject | — | `urgency` **fluid** |
| craw · T1 | ✅ | ✅ **pass** | — | `urgency` **static** |
| base · T2 | ✅ | ❌ reject | ❌ **`channel` fluid** | `channel` fluid, `message` fluid |
| craw · T2 | ✅ | ❌ reject | ✅ **`channel` static** | `channel` static, `message` fluid |

## Headline

- **Safety (the point):** on T2's injection boundary, base left the sink target `channel`
  **fluid** (untrusted text could choose where the notification posts); craw made it
  **static**. Biggest delta in the study.
- **Correctness:** craw·T1 is gate-clean; base·T1 is not.
- **Cost:** craw adds a modest authoring premium (T2: +27% cost, +80% wall-clock) for reading
  skills + self-checking.
- **Gap:** craw·T2 nailed the security-critical sink target but left `message` fluid → still
  fails the *bare* gate. A repair loop (`craw code fix`) would close it cheaply.
