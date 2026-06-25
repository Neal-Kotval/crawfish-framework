---
name: crawfish-authoring-knowledge
description: >
  Attach knowledge with Wiki / with_context / SkillRef — summoned content is tainted data,
  never instructions, and is pinned by content hash. Load when giving an agent a rubric,
  corpus, or reference it should consult.
user-invocable: false
allowed-tools: Read, Grep
---

# Authoring knowledge — `Wiki` / `with_context`

Derived from `docs/specs/craw-code/authoring/knowledge.md`. Golden:
`demo/craw-code-golden/knowledge.py`.

Knowledge is attached by **composition**, not as a directory file: build a `Wiki`, freeze it,
and summon it into a Definition with `with_context`. The result is a new frozen Definition
that carries a pinned reference to the knowledge — the body is not copied inline.

```python
from crawfish.wiki import Wiki, TrustTier
from crawfish.derive import with_context, SummonMode

wiki = Wiki().with_page(
    "triage-rubric",
    "P0 = outage; P1 = broken core flow; P2 = degraded; P3 = cosmetic.",
    trust=TrustTier.TRUSTED,        # first-party curated; STILL summoned tainted
)
specialized = with_context(base_definition, wiki, mode=SummonMode.READONLY)
```

## Summoned knowledge is tainted data

`wiki.consult()` materialises each page as a **tainted** context entry: knowledge arrives as
data the agent reads, never an instruction surface, and can never reach an instruction slot
or a static-only sink. Even `TrustTier.TRUSTED` content is summoned tainted — the trust tier
only ever *raises* suspicion. Taint can be dropped only via an audited `declassify`,
unreachable from a fluid path.

> **Spine rule (knowledge-is-tainted):** Summoned knowledge is tainted data, never an
> instruction surface.

## Pinned by content hash

`with_context` stores a `SummonRef` — `{id, version, mode}` — where `version` is the Wiki's
content sha snapshotted at compose time. `export().checksum` moves iff the pinned summon
version moves, and the summoned body never enters the export checksum. A frozen (eval-mode)
Wiki is required for a stable summon; `readonly()` seals an unfrozen Wiki first.

> **Spine rule (pinned-by-hash):** Knowledge is summoned by pinned content hash; the body
> never enters the export checksum.
