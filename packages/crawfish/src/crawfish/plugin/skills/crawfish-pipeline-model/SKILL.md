---
name: crawfish-pipeline-model
description: >
  The Crawfish pipeline mental model — Source → Filter → Batch → Aggregator → Router → Sink,
  and the Definition directory contract. Load when designing a pipeline or deciding between
  batch fan-out, an aggregator reduce, a router branch, or a refine loop.
allowed-tools: Read, Grep
---

# The Crawfish pipeline mental model

A Crawfish project is **agents that do bulk work over your data**. The pipeline shape:

```
Source → Filter → Batch (fan-out) → Aggregator (reduce) → Router (branch) → Sink
```

Each node has one job:

- **Source** — produces the items to work over (fluid-source fan-out **originates taint**).
- **Filter** — drops items before fan-out (cheap pre-pass).
- **Batch (fan-out)** — runs the Definition once **per item**, independently. The workhorse.
- **Aggregator (reduce)** — folds many per-item outputs into one. **Taint is the union** —
  the fold is tainted if any input was (security rule 9).
- **Router (branch)** — branches on a label. A model-derived label may gate **whether** a
  consequential action fires, **never choose among** distinct consequential sinks.
- **Sink** — the consequential write. Its **`target` is static-only** (security rule 2); it
  fires only against a **frozen** (eval-mode) Definition (rule 7).

## The Definition directory contract

A Definition is a directory (see `docs/reference/definition.md`). One line each:

- `definition.py` — typed IO (`Parameter`, `Flow.STATIC` vs fluid) + team shape
  (`lead` / `coordination`). The spine's primary surface.
- `instructions.md` — the lead agent's prompt (system prompt).
- `agents/*.md` — subagents; each is front-matter (`role`, `delegates_to`, `tools`,
  `model`) over a markdown body.
- `tools/*.py` — host-side callables; the callable name = the filename stem. Taint-aware IO.
- `mcp/*.py` — `MCPConnection`s; `auth` is an env-var **reference**, never an inline value.
- `policies/*.py` — module-level `Policy` objects (budgets, retries, guards).
- `skills/*.md` — skill assets bundled with the Definition.
- `fixtures/` — recorded inputs/outputs for deterministic `craw test`.

For the file-by-file authoring rules, see the `crawfish-authoring` playbook skills (the
progressive-disclosure siblings shipped in this same plugin bundle).

## Decision guide — which shape do I reach for?

| Goal | Reach for | Why |
| --- | --- | --- |
| Per-item independent work over many items | **Batch fan-out** | each item is processed alone; no shared state |
| Combine many per-item results into one | **Aggregator (reduce)** | a fold/vote/summary; remember taint is the union |
| Take different action depending on a label | **Router (branch)** | the label *gates*; it never *chooses among* sinks |
| Iterate toward a goal or bound | **Refine loop** | `craw refine --until <goal/bound>`; feedback stays fluid + tainted |

**Worked example — triage many tickets.** A `Source` yields open tickets. A `Batch`
fan-out runs the `triage-bot` Definition once per ticket (the `ticket_body` is **fluid**,
the `project` is **static**). A `Router` branches on the model's `severity` label to decide
**whether** to open an issue (gating a consequential action) — it does not choose **which**
repo (that `target` is a static parameter). A `Sink` opens the GitHub issue against the
**static** `target`, under the idempotency key derived from static config + the item id.

## Coordination shapes

A Definition's team shape (`coordination` in `definition.py`):

- `single` — one agent does the whole task.
- `lead` — a lead agent delegates to named subagents (`agents/*.md`).
- `sequential` — agents run in a fixed pipeline order, each handing to the next.

Pick `single` for a one-shot transform, `lead` when work splits into specialist roles, and
`sequential` when stages must run in order.
