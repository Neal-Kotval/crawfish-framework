# Crawfish vs. naive Claude — tiered task (genuine quality headroom, live)

- **Backend:** real `claude -p` via `CommandRuntime` · primary=`haiku` strong=`sonnet` · concurrency=8
- **Task:** 18 purchase orders → tiered volume discount + 7.25% tax + rounding, with subtotals on tier boundaries. Ground truth computed in Python.
- **Cascade signal:** escalate to the strong model when the cheap model's `grand_total` doesn't follow from its own `subtotal` under the rule (`chain_inconsistent`) — an objective self-check, not self-reported confidence.

## Results

| Metric | haiku-only (naive loop) | sonnet-only (naive loop) | crawfish (parallel + haiku→sonnet cascade) |
|---|---|---|---|
| Accuracy | 0.722 | 0.889 | 1.0 |
| Correct | 13/18 | 16/18 | 18/18 |
| Total cost (USD) | 0.7856 | 2.2602 | 1.5858 |
| Wall-clock (s) | 145.9 | 96.2 | 31.8 |
| Model calls | 18 | 18 | 24 |
| Escalations | 0 | 0 | 6 |

## Read-out — crawfish wins on all three axes

- **Quality:** crawfish **1.0** vs cheap-only **0.722** — a **+0.278 accuracy lift** by escalating the 6 self-inconsistent item(s) to the strong model. (strong-only: 0.889.)
- **Cost:** **$1.5858** vs **$2.2602** strong-only — **~30% cheaper**: only the hard tail paid for the strong model.
- **Speed:** **31.8s** vs **96.2s** strong-only and **145.9s** cheap-only — **~3.0× faster** than strong-only via parallel fan-out.

## Why this task (and the honest boundary of the result)

Earlier tasks (classification, plain multi-item totals) hit the quality ceiling — `claude -p` runs even haiku with extended thinking, so there was nothing to recover and the cascade never fired. Quality headroom only appears on genuinely harder work: here, chained conditional arithmetic with boundary cases, where the cheap model drops to 0.722. **The framework's quality win is real but conditional** — it shows up exactly when (a) the cheap model actually errs and (b) errors are detectable by a cheap check. Both hold here by construction; on ceiling tasks the same machinery is correct but idle.

## Caveats

1. **The cost win depends on the escalation rate.** Here 6/18 (33%) escalated, so most items ran cheap and crawfish beat sonnet-only on cost. On an all-hard variant (no easy tail) the cheap model failed ~60% → ~80% escalated → crawfish cost *more* than sonnet-only. The cascade pays off only when the cheap model handles the majority; match the primary model to the workload's difficulty mix.
2. **N=18.** crawfish edging strong-only on accuracy (1.0 vs 0.889) is within run-to-run noise — read it as "matched the strong tier," not a durable beat.
3. Cost/tokens are inflated by the ~37k-token context the local Claude Code install loads per `claude -p` call (every condition pays it).
4. The cascade depends on a cheap, reliable error signal. Here it's an exact self-consistency check; tasks without one fall back to validation-failure / confidence (weaker, as the classification runs showed).
5. A hand-rolled loop could implement the same cascade — crawfish provides it as a tested seam (`EscalatingRuntime`) plus parallelism, budget, and typed validation, so you don't rebuild the plumbing per project.