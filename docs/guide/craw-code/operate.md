# Operate and optimize

Once a project is authored, `craw code` runs it: it deploys pipelines, watches the fleet,
optimizes components against a budget, and lets you cancel or resume a run without paying twice
for work already done. This page covers the operate plane — and the one rule that runs through
all of it: optimization *proposes*, it never auto-promotes.

!!! note "You will learn:"
    - How `optimize` searches for a better component without ever promoting one
    - `deploy` and `fleet` — putting a pipeline into service and watching it
    - `cancel` and `resume` — cooperative cancellation and `$0` resumption
    - How cost ceilings bound every operate verb

!!! note "Operate-plane verbs"
    The verbs on this page (`optimize`, `deploy`, `fleet`, `cancel`, `resume`) are part of the
    operate/HITL plane. They are documented here, matching [RFC 0001](../../rfcs/0001-craw-code.md);
    if a verb is not yet registered in your build, `craw code --help` will tell you. The
    schemas and exit codes below match the spec.

## Optimize, never auto-promote

`craw code optimize <component>` orchestrates the tune / refine / learn loop over a component,
under a budget, and **stages the result as a proposal**. It never swaps the live component for
the candidate on its own.

```bash
craw code optimize definitions/triage --mode auto --budget 5.00
# scaffolds tune.toml, seeds a baseline, searches in eval-mode (fires no Sink)
```

| Flag | What it does |
| --- | --- |
| `--mode {tune,refine,learn,auto}` | Which optimization strategy to run; `auto` lets the loop choose. |
| `--budget USD` | The spend ceiling for the search; the loop halts when it is reached. |

Two properties make this safe to run unattended:

- It runs in **eval-mode**, so the search fires no consequential Sink. A candidate that would
  send a Slack message during evaluation cannot — evaluation reads and scores, it does not act.
- Its output is a **proposal**, not a promotion. The improved candidate is staged for the
  [review-and-approve gate](review-and-approve.md); a human approves the swap, or it never
  happens.

!!! danger "Promotion is gated, always"
    There is no flag that promotes an optimized candidate live in one step. Promotion goes
    through `propose` → human approval → `apply`. The gate [fails closed](review-and-approve.md):
    a PreToolUse hook hard-denies an un-approved `--live` or sink-firing call even under
    `bypassPermissions`.

## Deploy and fleet

`craw code deploy` puts a pipeline into service together with its default observers, and
`craw code fleet` is the operator's view of what is running:

```bash
craw code deploy pipelines/triage           # deploy a pipeline + default observers
craw code fleet list                         # what is running, with status
craw code fleet tail <run_id>                # follow a run's events
craw code fleet stop <run_id>                # stop a deployment
```

Both emit the `craw.code.fleet.v1` envelope under `--json`, so a script can read fleet state
the same way a human reads the [dashboard](dashboard.md). The dashboard, in fact, is the
read-model over exactly these events.

## Cancel and resume

A long batch can be stopped cooperatively and picked back up without re-paying for finished
work:

```bash
craw code cancel <run_id>      # cooperative cancel via the run's CancelToken
craw code resume <run_id>      # re-enter the ledger path; DONE items cost $0
```

`cancel` signals the run's `CancelToken`; the run stops at the next cooperative checkpoint
rather than being killed mid-item. `resume` re-enters the same ledger path the run was on —
items already recorded `DONE` are not re-run, so resumption costs `$0` for completed work.
Both carry the `craw.code.control.v1` envelope.

## Cost ceilings bound everything

Every operate verb respects a budget, and the budget is honest. `estimate` (see the
[CLI page](cli.md)) prices a run before you start it with a true band — `total_usd` lower
bound, `worst_case_usd` upper bound, `expected_usd` between — so you set a ceiling against the
worst case, not a hopeful average:

```bash
craw code estimate pipelines/triage --items 500 --json
# preview the band over 500 items before deploying
```

The project `[budget]` ceiling in `crawfish.toml`, and any `--budget` you pass, halt a run
with exit `3` (`budget_exceeded`) the moment the ceiling is crossed. `optimize` halts its
search the same way. A budget is not advisory; it is a hard stop.

## See also

- [Review & approve (HITL)](review-and-approve.md) — the gate every promotion passes through
- [The dashboard](dashboard.md) — the read-model over the events these verbs produce
- [The craw code CLI](cli.md) — exit codes and the `--json` envelope
- [Drive Crawfish from the CLI](../optimize-from-the-cli.md) — the underlying score/search/refine/learn loop
