# craw code — Benchmarks: base Claude Code vs craw code

A set of **one-shot benchmarks** comparing a vanilla Claude Code agent ("base") against the
same agent equipped with **craw code** (authoring skills + the `craw code` CLI + the jail and
assembly-gate enforcement), plus an analysis of which features would move each metric.

> **Status: a small real pilot (n = 1 per cell), run live with `claude -p --output-format
> json`.** The numbers below are *measured*, not invented — but n = 1 has high variance, so
> read them as direction and signal, not a leaderboard. The harness in
> [`bench/authoring/`](../../../bench/authoring) reproduces and scales this; "Features" below
> includes making it a first-class `craw code bench`.

> **This is the *authoring-layer* benchmark** (does craw code help an agent *write* a safe,
> correct Crawfish project?). The complementary *operate-layer* benchmark — does the crawfish
> runtime beat plain `claude -p` over a bulk workload? — already lives in
> [`bench/`](../../../bench) and is **measured live**: via the escalating cascade, crawfish
> reaches strong-model quality at **~64% lower cost and 2.5–5× faster** (see
> `bench/RESULTS_3WAY.md`). Together they cover the two halves: *write it* and *run it*.

## What we're comparing

Both arms are `claude -p "<task>" --allowedTools Read,Write,Edit,Bash`. Same task text per
pair. The **only** difference is the treatment:

| Arm | Gets |
| --- | --- |
| **base** (Claude Code) | The task + a minimal Crawfish API hint. No skills, no CLI, no self-check. |
| **craw code** | The same, **plus** instructions to read the `crawfish-security-spine` and `crawfish-authoring-definition-py` skills, **plus** permission to self-check with `craw code describe --json`. |

This isolates the value of the craw code *teaching + tooling* — not just "knows the API."

## The tasks (one-shot authoring)

| ID | Task | What it probes |
| --- | --- | --- |
| **T1 classifier** | Author a definition that classifies a ticket's urgency from a fluid `ticket_body` + static `project`. | Baseline competence + getting `Flow` tags right. |
| **T2 sink-safety** | Author a definition that posts a notification to a Slack **channel** decided from a ticket. | The injection boundary: is the consequential **sink target** (`channel`) kept `Flow.STATIC`, or can untrusted data choose where a write lands? |

## Metrics

| Metric | Definition | Source |
| --- | --- | --- |
| **Cost** | USD for the authoring run | `total_cost_usd` |
| **Speed** | wall-clock seconds; agent turns | `duration_ms`, `num_turns` |
| **Tokens** | input / output tokens | `usage` |
| **Compiles** | loads under the jail (`load_definition_jailed`, `denied=False`) | craw pipeline |
| **Gate-clean** | passes the ALG-3 assembly gate (`assert_build_safe` → no reject) | craw pipeline |
| **Sink-target-safe** | the consequential sink target is `Flow.STATIC` (untrusted data can't steer the write) | inspection |

Other dimensions discussed under *Analysis*: reliability/variance, reproducibility,
cost-at-scale, auditability.

## Pilot results (measured)

**Speed / cost / tokens** (`claude -p` json, one run each):

| Cell | Cost | Wall-clock | Turns | In tok | Out tok |
| --- | --- | --- | --- | --- | --- |
| base · T1 | $0.471 | 36.6 s | 6 | 18,734 | 1,872 |
| craw · T1 | $0.495 | 31.6 s | 6 | 18,732 | 1,796 |
| base · T2 | $0.545 | 47.3 s | 8 | 18,463 | 2,360 |
| craw · T2 | $0.692 | 84.9 s | 10 | 18,867 | 4,843 |

**Quality** (the authored output, run through the real craw pipeline):

| Cell | Compiles | Gate-clean | Sink-target-safe | Output flows |
| --- | --- | --- | --- | --- |
| base · T1 | ✅ | ❌ reject | — (no real sink) | `urgency` **fluid** |
| craw · T1 | ✅ | ✅ **pass** | — | `urgency` **static** ✓ |
| base · T2 | ✅ | ❌ reject | ❌ **`channel` fluid** (injectable) | `channel` fluid, `message` fluid |
| craw · T2 | ✅ | ❌ reject | ✅ **`channel` static** | `channel` **static**, `message` fluid |

### What this says

1. **The teaching works on the metric that matters most — safety.** On T2 (the injection
   boundary), base left the **sink target `channel` fluid** — untrusted ticket text could
   choose which Slack channel gets posted to. craw, steered by the spine skill, made
   `channel` **static**. That is the single highest-value delta in the whole study, and it's
   exactly the failure the framework exists to prevent.
2. **craw also lifts plain correctness.** On T1, craw produced **gate-clean** code (`urgency`
   static); base did not (`urgency` fluid → rejected).
3. **craw costs a modest authoring premium.** T1 was a wash (~$0.48, ~same time); T2 cost
   **+27% ($0.55 → $0.69)** and **+80% wall-clock (47 s → 85 s)** because the agent read two
   skills and self-checked. The overhead grows with task subtlety — and buys correctness.
4. **Even craw isn't fully gate-clean on T2.** It nailed the *security-critical* part
   (static sink target) but left `message` fluid, so the *bare* gate still rejects. A
   near-miss a repair loop would close in one cheap step (see Features → A).
5. **A real usability bug surfaced.** Pointing the craw arm's self-check at an absolute repo
   path made the agent author **into the repo** instead of its own cwd. Lesson: the
   self-check must target the agent's own project dir (Features → B).

## Analysis: the dimensions a single authoring run doesn't show

- **Cost at scale (operate, not author).** Authoring is a **one-time** ~$0.5–0.7. The base
  agent doing the *same work ad-hoc* re-pays full LLM cost **per item, every time**, and the
  result is non-reproducible. craw authors once, then runs the typed definition over N items;
  **replay is $0** (content-hash verified) and the tuner can drive per-item cost down. This is
  not hand-waving — the existing operate-layer benchmark (`bench/RESULTS_3WAY.md`) **measures**
  it: at equal (ceiling) quality, the crawfish cascade ran **~64% cheaper and 2.5–5× faster**
  than a naive per-item `claude -p` loop. For a single one-off item ad-hoc is cheaper; for
  repeated / bulk / auditable work craw amortizes and wins — and is strictly ahead on
  reproducibility and safety at any N.
- **Reproducibility.** base: none (every run is a fresh sample). craw: record/replay makes a
  run bit-identical and free to re-run; you can snapshot and assert in CI.
- **Auditability.** base: a transcript. craw: a typed, versioned, diffable artifact + an
  event ledger + a lockfile.
- **Variance.** n = 1 hides it. The harness supports `pass@k` over k seeds; the spread on the
  *quality* metric (gate-clean rate) is the number worth tracking.

## Features to add — to improve each metric

Prioritized by leverage (impact × how cleanly it bolts onto existing seams).

### A. Authoring quality / `pass@1` — **highest leverage**
- **`craw code fix` / a Refine repair loop.** Feed the structured `craw.error.v1` reject
  (offending Parameter + remediation) back to the agent — or auto-apply the mechanical fix
  (flip a non-target output to `STATIC`, or mark a sink target) — and re-gate, bounded by
  `max_iters`/budget. Turns craw·T2's near-miss into a pass without a second full authoring
  round. Reuses the existing `Refine` operator + the assembly gate.
- **Actionable gate errors.** The reject already carries `remediation`; include the *exact*
  offending `Parameter` name and the one-line edit ("make `channel` Flow.STATIC") so the agent
  self-corrects *in the same turn*. Cheap, big `pass@1` gain.
- **Sink-target inference.** Statically classify which outputs are consequential sink targets
  vs content, so the gate can be precise instead of "all outputs must be static" — this is
  what made craw·T2 fail on `message` despite being safe.
- **Golden-shaped few-shots in the skill.** Embed the gate-clean golden snippet so the agent
  pattern-matches the right shape instead of deriving it.

### B. Authoring cost / speed overhead
- **Skill distillation.** Ship a compact "spec card" (the existing `authoring-spec.toml`
  machine form) the agent reads instead of full prose — cuts the +27%/+80% T2 overhead.
- **Prompt caching** across a batch of authoring calls (Anthropic prompt caching on the
  shared skill/system prefix).
- **Edit-don't-author.** `craw code new` already scaffolds a spine-correct template; steer the
  agent to *edit* it (less generation = fewer output tokens) rather than write from scratch.
- **Fix the self-check cwd bug** (B/finding 5): the skill's self-check must use the agent's
  own project dir / a relative path, never an absolute repo path.

### C. Operating cost at scale
- **Tuner-driven model downgrade** (have `Tuner`): search for the cheapest model/prompt that
  still clears the eval bar, promote the winner.
- **Per-item routing + Escalate** (have `RoutingRuntime`/`Escalate`): cheap model first,
  escalate only on low confidence.
- **Prefix/KV caching across batch items** (shared system prompt over a fan-out).

### D. Security / injection — push craw's strongest dimension further
- **Real out-of-process OS jail** (the standing follow-up): `SandboxPolicy(kind="fake")`
  certifies-then-imports in-process; a real jail backend is required before any live host
  execution.
- **Injection-resistance benchmark** as a first-class suite (poisoned `ticket_body` that tries
  to flip the channel / leak a secret / inject instructions) with a measured resistance rate.

### E. Make the benchmark a product feature — **`craw code bench`**
- A verb that runs a task matrix across arms, captures cost/latency/turns (from `claude -p`
  json) + quality (gate / eval / golden) + `pass@k` over seeds, and emits a leaderboard +
  `craw.code.bench.v1` JSON. Wire it into CI as a **regression gate** (authoring `pass@1` and
  injection-resistance must not drop). This turns "compile a set of one-shot benchmarks" from
  a manual study into a button.
- A standard **task taxonomy + golden corpus**: author-classifier, author-sink-safety,
  author-tooluse, author-mcp-consent, operate-bulk, operate-optimize, injection-resistance,
  repro-replay.

## Limitations (be honest)
- **n = 1 per cell.** Directionally clear (especially the T2 safety delta), but not
  statistically robust. Run the harness with `K≥5` for real intervals.
- Two task types, authoring-only — operate/bulk metrics here are modeled, not measured.
- The "base" arm gets a minimal API hint; a base agent allowed to read the full docs would
  likely close some of the correctness gap (but not for free — that's reading cost, and it
  still has no enforcement).

## Reproduce / extend
See [`bench/authoring/`](../../../bench/authoring): `author_bench.sh` runs the arms,
`evaluate.py` scores the outputs against the real pipeline. **It makes live `claude -p` calls
(costs money) and is intentionally outside the pytest suite.** The operate-layer suite next to
it (`bench/run.py`, `bench/bench3.py`) covers the run-it side.
