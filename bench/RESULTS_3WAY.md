# Crawfish vs. naive Claude — three-way (hard task, live)

- **Backend:** real `claude -p` via `CommandRuntime` · primary=`haiku` strong=`sonnet` · concurrency=8
- **Workload:** 14 hard triage tickets (surface-vs-intent tension) with ground-truth labels
- **Conditions:** cheap-alone · strong-alone · crawfish (parallel fan-out + confidence-gated cascade + typed validation)

## Results

| Metric | haiku-only (naive loop) | sonnet-only (naive loop) | crawfish (parallel + haiku→sonnet cascade) |
|---|---|---|---|
| Accuracy | 1.0 | 1.0 | 1.0 |
| Schema-valid | 14/14 | 14/14 | 14/14 |
| Total cost (USD) | 0.6034 | 1.7178 | 0.6263 |
| Input tokens | 519,857 | 511,037 | 531,221 |
| Output tokens | 6,755 | 1,111 | 6,782 |
| Wall-clock (s) | 152.6 | 73.3 | 29.0 |
| Model calls | 14 | 14 | 14 |
| Escalations | 0 | 0 | 0 |

## Read-out

- **Speed:** crawfish ran in **29.0s** vs **73.3s** for the strong-only loop — **~2.5× faster** from parallel fan-out (`batch.py`), at concurrency 8. The naive loops are sequential.
- **Cost:** crawfish spent **$0.6263** vs **$1.7178** strong-only — **~64% cheaper** — because the cheap model handles the batch and only 0 item(s) escalated.
- **Quality:** crawfish **1.0** vs strong-only **1.0** vs cheap-only **1.0**. Parity — downgrading lost no accuracy.

## Honest notes

1. **Modern models are at the quality ceiling for this task.** Even the cheap model scored 1.0, so the cascade rarely needs to escalate (0 escalation(s)). The win is **cost + speed at equal quality**, not a quality jump — there was little headroom to win. Self-reported confidence is a weak signal (the cheap model is overconfident), so escalation here is a safety net, not a frequent path.
2. **The speed win is real and general**; the cost win depends on the cheap model matching the strong one on your task — true here, verify on yours.
3. **Token/cost are inflated** by the ~37k-token context the local Claude Code install loads per `claude -p` call (both conditions pay it); raw-API numbers are lower.
4. A *good* hand-rolled loop could also parallelize — crawfish's value is doing it **safely** (shared budget enforced under concurrency, typed validation, ordered results) without you writing that plumbing.