# Review and approve (HITL)

`craw code` lets an agent author and operate a project, but it does not let an agent decide on
its own to do something consequential. Between a proposed change and a live promotion stands a
human-in-the-loop gate: the agent stages a typed diff, a person reviews it, and only a recorded
approval lets it apply. The gate fails closed — without an approval on file, the promotion does
not happen, even under permissive run modes.

!!! note "You will learn:"
    - `review` and `diagnose` — the digests that tell you what needs attention
    - The `propose` → approve → `apply` gate, and how it is keyed
    - Why the gate fails closed, and what enforces it

!!! note "HITL-plane verbs"
    The verbs on this page (`review`, `diagnose`, `propose`, `apply`) are part of the
    operate/HITL plane, documented here, matching [RFC 0001](../../rfcs/0001-craw-code.md).
    Check `craw code --help` for which are registered in your build; the schemas and exit
    codes below match the spec.

## Reviewing what happened

`craw code review` turns the ledger, observer events, and the dead-letter queue into a ranked
digest, each finding carrying a deterministic `suggested_action`:

```bash
craw code review --since 24h --json
# ranked findings from the ledger/observers/DLQ, each with a suggested action
```

When a specific run went wrong, `craw code diagnose <run_id>` correlates that run's `RunInfo`,
its observer events, the DLQ entry, and the failing typed IO into a root cause — and proposes a
`$0` remediation you can replay:

```bash
craw code diagnose <run_id> --json
# correlates run + observers + DLQ + failing IO → root cause + craw replay --swap remediation
```

Both are read-only. They tell you what to do; they do not do it.

## The propose → approve → apply gate

A consequential change — promoting an optimized candidate, enabling a live run — is staged,
not applied. `craw code propose` records a typed diff plus a cost estimate, keyed to the exact
`(component, content_sha)` it was computed against:

```bash
craw code propose definitions/triage --json
# stages a typed diff + cost estimate, keyed (component, sha)
```

A human reviews the staged diff and either approves it or rejects it. Only then does `apply`
do anything:

```bash
craw code apply definitions/triage      # applies only if the (component, sha) is approved
craw code apply definitions/triage      # -> exit 7 (no approval) if it is not
```

If the proposal is rejected, the rejection is recorded and `LearningLoop.rollback` returns the
component to its prior state at `$0`. Because the approval is keyed to the content sha, an
approval cannot be reused for a *different* change — editing the component after approval
invalidates the key, and `apply` refuses again.

| Verb | What it does | Exit on no-approval |
| --- | --- | --- |
| `propose` | Stage a typed diff + cost estimate, keyed `(component, sha)`. | — |
| `apply` | Apply the staged change **only** if its key is approved. | `7` (no approval), `8` (ceiling reached) |

## Failing closed

The gate is not a convention the agent is asked to honor — it is enforced underneath the
agent, so an injected or confused agent cannot route around it.

!!! danger "No promotion without a recorded human approval"
    A PreToolUse hook hard-denies any un-approved `--live` or sink-firing call with exit `2` —
    **even under `bypassPermissions`**. There is no run mode, and no agent instruction, that
    promotes a candidate or fires a consequential sink without an approval recorded against its
    content sha. The default is no, and the only thing that changes it is a human.

This is the same principle as the rest of the [security model](security.md): the controls that
matter are enforced by construction, not advised in a skill an injected agent could be talked
out of.

## A deterministic walk-through

You can exercise the gate end to end on the demo without a live model. Stage a proposal,
inspect it, then try to apply it before and after approval:

```bash
craw code optimize demo/craw-code-golden --mode tune --budget 2.00   # eval-mode, fires no sink
craw code propose demo/craw-code-golden --json                       # stage the diff + cost
craw code apply demo/craw-code-golden                                 # -> exit 7: no approval yet
# a human approves the staged (component, sha) ...
craw code apply demo/craw-code-golden                                 # now it applies
```

The first `apply` failing with exit `7` is the gate working: the proposal exists, the cost is
known, and the change still does not happen until a person signs off.

## See also

- [Operate & optimize](operate.md) — where proposals come from (`optimize` stages, never promotes)
- [Security model](security.md) — the fail-closed principle in depth
- [The craw code CLI](cli.md) — the exit codes (`7` no-approval, `8` ceiling-reached)
- [Diff, prove, and replay](../diff-prove-replay.md) — the `$0` replay the remediation uses
