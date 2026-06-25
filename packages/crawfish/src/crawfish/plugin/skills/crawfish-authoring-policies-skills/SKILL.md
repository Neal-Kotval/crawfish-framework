---
name: crawfish-authoring-policies-skills
description: >
  Author policies/*.py (module-level Policy instances — consequential static config) and
  skills/*.md (bundled skills). Load when adding a guardrail/routing/permission policy or a
  bundled skill. A policy is static-only.
user-invocable: false
allowed-tools: Read, Grep
---

# Authoring `policies/*.py` & `skills/*.md`

Derived from `docs/specs/craw-code/authoring/policies-skills.md`. Golden:
`demo/craw-code-golden/policies/spend_guard.py` + `skills/*.md`.

## `policies/*.py` — module-level `Policy` instances

Each `policies/*.py` declares one or more **module-level `Policy` instances**. The compiler
discovers them into `DefinitionAssets.policies`; an agent binds one by name in front-matter
`policies: [...]`. Binding an unknown policy fails at load with `DefinitionLoadError`.

```python
"""spend_guard — a guardrail Policy: a per-batch model-spend cap."""
from __future__ import annotations
from crawfish.core import Policy, PolicyKind

spend_guard = Policy(
    name="spend_guard",
    kind=PolicyKind.GUARDRAIL,
    rules={"max_usd_per_batch": 5.0},
)
```

A `Policy` is constructed with `name`, a `kind` (`PolicyKind.GUARDRAIL` / `ROUTING` /
`PERMISSION`), and a `rules` dict. (`Policy` has **no** `description` field — it is `name` +
`kind` + `rules`.)

## A policy is consequential, therefore static-only

A `Policy` is consequential, static config — what an agent may or may not do, spend caps,
which sources/sinks it may touch. It is never derived from a fluid or model-derived value.
`with_policy` adds a *static* policy and folds it into the content sha.

> **Spine rule (consequential-static-only):** Consequential sink targets, idempotency keys,
> and consequential outputs are static-only.

## `skills/*.md` — bundled skills

Each `skills/*.md` is a bundled skill the Definition carries; the compiler discovers them by
filename into `DefinitionAssets.skills`. A skill is a markdown body (optionally with
front-matter) — reusable instructions, not a consequential slot.
