---
name: crawfish-authoring-optimizing
description: >
  Optimize a Crawfish component — set a baseline (craw eval), search knobs (craw tune),
  iterate (craw refine), self-version (craw learn). Train vs eval mode, budget-bounded.
  Load when improving a component's quality or cost.
user-invocable: false
allowed-tools: Read, Grep, Bash
---

# Optimizing a component

Derived from the authoring playbook (UNFILED-OPT). Pairs with the `craw code optimize`
orchestrator. Every example is **seeded** and **budget-bounded**.

## Train vs eval mode

A Definition is either **eval mode** (frozen — it gates, it never searches) or **train mode**
(mutable — the knob space is searched). A frozen eval-mode Definition is the reproducible
artifact; you enter train mode to optimize, then promote a winner back to a frozen artifact.

A consequential **sink never fires during optimize** — optimize/search runs in eval-only
posture, so a tune/refine loop never performs a real write. Determinism: the same `--seed`
yields a byte-identical `winner` sha.

## The arc: baseline → tune → refine → learn

1. **Seed a baseline** — `craw eval --set-baseline` records the score + cost band to gate
   against.
2. **Search knobs** — `craw tune --models … --max-trials N --cost-per-trial … --budget … \
   --cost-regularized`: a cost-regularized objective searches the tunable knob space.
3. **Iterate to a goal** — `craw refine --until "score>=0.95" --budget …`: a verifier-gated,
   bounded, durable loop.
4. **Self-version** — `craw learn` runs one self-versioning cycle with rollback.

## Always bounded

Never run an unbudgeted `--live` search. Every search and refine is under `--budget`, and the
cost band (`total_usd` / `expected_usd` / `worst_case_usd`) is read before promoting. A
security rejection (`retryable:false`) stops the loop — it is never retried past.
