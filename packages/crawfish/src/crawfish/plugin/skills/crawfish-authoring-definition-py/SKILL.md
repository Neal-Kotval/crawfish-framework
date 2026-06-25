---
name: crawfish-authoring-definition-py
description: >
  Author definition.py — typed inputs/outputs (Parameter, static vs fluid) and the team
  shape (lead/coordination). Load when writing or editing a Definition's typed boundary.
  Static is the deliberate consequential choice; fluid is the untrusted default.
user-invocable: false
allowed-tools: Read, Grep
---

# Authoring `definition.py`

Derived from `docs/specs/craw-code/authoring/definition-py.md` (the single source of truth).
Golden: `demo/craw-code-golden/definition.py`.

`definition.py` declares the Definition's **typed boundary** (`inputs` / `outputs` as
`Parameter`s) and the **team shape** (`lead` / `coordination`). This is the spine's primary
surface: here you decide what is trusted config and what is untrusted data.

```python
from __future__ import annotations
from crawfish.core import Flow, Parameter

inputs = [
    Parameter(name="project", type="str", flow=Flow.STATIC),   # set once at batch start
    Parameter(name="ticket_body", type="str"),                 # default → FLUID (per-item)
]
outputs = [Parameter(name="triage", type="Triage", flow=Flow.STATIC)]
lead = "lead"
```

## Static vs fluid

- **`Flow.STATIC`** is the deliberate, consequential choice: author config set once at batch
  start — a project id, a sink destination, an idempotency input.
- **Fluid is the default.** Omit `flow` and a `Parameter` is `Flow.FLUID`: untrusted,
  per-item session data.

> **Spine rule (fluid-is-data):** A Flow.FLUID value reaches the model as data, never as
> instructions.

## The consequential output is static-only

A consequential **output** is declared `Flow.STATIC`. The assembly gate (ALG-3,
`assert_build_safe`) treats a `Flow.FLUID` output as a suspected fluid-fed target slot and
**fails closed**. In the golden, `triage` is the team's decision written into a static slot,
so it is `Flow.STATIC` and the build gate discharges it.

> **Spine rule (consequential-static-only):** Consequential sink targets, idempotency keys,
> and consequential outputs are static-only.

Never derive a static slot from a fluid value. A `with_*` composition op never widens
fluidity — it carries each `Parameter`'s flow through unchanged.

## Team shape

`lead = "lead"` names the coordinator; with more than one agent the compiler infers
`Coordination.LEAD`. Set `coordination` (`single` / `lead` / `sequential`) to override. The
`lead` and every `delegates_to` target must be a real team role — an unknown role fails at
load with `DefinitionLoadError`.
