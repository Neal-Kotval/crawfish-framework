# Agents as variables — compose, version, and summon knowledge

The tunable-ML half made an agent a **model with tunable weights**. This page makes it a
**variable**: a content-addressed value you compose from parts, give a *name*, and move
through a version log — *git for agents*. Alongside it, knowledge becomes something you
**summon** by reference into a loop, so it reaches the model as **data**, never as
instructions.

Three moves, all real public API importable from the top-level `crawfish` package, all
deterministic under `MockRuntime`:

- **Compose** a variant with copy-on-write operators — `with_skill`, `with_context`,
  `with_agent`. Each returns a **new frozen** Definition; the receiver is never mutated.
- **Version** it by name — `DefinitionStore.save` / `recall`, then `modify` / `reset`
  over the append-only version log. A name is a *mutable pointer* into an *immutable,
  content-addressed object store* — git's exact ergonomic.
- **Summon** knowledge — a `Wiki` is a versioned, content-hashed knowledge unit you pin by
  sha and `consult()` as tainted context.

On this page:

- [Compose a variant — `with_*`](#compose-a-variant-with_)
- [Name and version it — `save` / `recall`](#name-and-version-it-save-recall)
- [`modify` / `reset` — the remaining git verbs](#modify-reset-the-remaining-git-verbs)
- [Summon knowledge — the `Wiki`](#summon-knowledge-the-wiki)
- [What stays static (the security spine)](#what-stays-static-the-security-spine)

## Compose a variant — `with_*`

A compiled `Definition` is `Freezable`. The `with_*` operators are **copy-on-write**: each
takes a base, makes a deep unfrozen copy, applies one structural edit, and re-seals it with
a **fresh content hash**. The base is never touched, and the returned value is already
frozen (eval-mode).

```python
import crawfish as cw

base = cw.eval(cw.Definition.from_package("demo/triage-bot"))   # eval() == freeze

# Acquire a skill by version pin (reference-not-embed: folded into dependencies).
variant = cw.with_skill(base, cw.SkillRef(id="label-taxonomy", version="1.0"))

# Add a team agent (replace=True swaps a same-role agent instead of adding).
variant = cw.with_agent(variant, reviewer_agent)

assert variant.frozen                       # the result is sealed
assert variant.content_sha() != base.content_sha()   # one knob moved → a new sha
assert base.content_sha() == base.content_sha()       # the base is unchanged
```

Two structurally identical compositions collapse to **one** sha (idempotent); any knob diff
diverges it. Because every op routes through the single content-hash path, un-versioned
mutation is impossible — `with_*` on a frozen receiver copies first (never raises
`FrozenError`), but mutating the **returned** frozen object does.

The available operators:

| Operator | Adds |
| --- | --- |
| `with_skill(base, SkillRef)` | a skill by version pin |
| `with_agent(base, agent, *, replace=False)` | a team agent (or swaps a same-role one) |
| `with_context(base, summonable, *, mode=SummonMode.READONLY)` | a summoned knowledge unit, pinned by version |
| `with_inputs(base, *params)` | widens the typed input surface (never widens fluidity) |
| `with_policy(base, policy)` | a static consequential policy |

`with_context` takes anything `Summonable` (structural typing — see
[Concepts → the static-vs-fluid boundary](concepts.md#the-static-vs-fluid-prompt-injection-boundary));
a `Wiki` is the built-in one, covered [below](#summon-knowledge-the-wiki).

## Name and version it — `save` / `recall`

Composition gives content **hashes** but no **names**. `DefinitionStore` is the name
registry — a `Store`-backed, append-only, org-scoped `name → hash` pointer over an
immutable object store.

```python
from crawfish.store import SqliteStore

ds = cw.DefinitionStore(SqliteStore("agents.db"), org_id="local")

sha = ds.save("triage", base)               # requires a frozen (eval-mode) Definition
assert ds.recall("triage").content_sha() == sha   # recall the latest

ds.save("triage", variant, parent=sha)      # move the name pointer to the variant
assert len(ds.log("triage")) == 2           # the append-only lineage, oldest→newest
assert ds.head("triage") == variant.content_sha()
```

`save` does exactly one mutation — it moves the name pointer — then appends a
`DefinitionVersion` lineage event carrying the `parent` edge. The body is stored
**content-addressed**, so two byte-identical saves dedup the object but still record two
pointer events. `recall` is **pure**: it reads a stored object, re-seals it frozen, and
**never mints a new sha**. A historical version stays reachable by pin:

```python
old = ds.recall("triage", sha=sha)          # or recall("triage@<sha>"), or a bare sha
assert old.content_sha() == sha             # reachable after the pointer moved on
```

Saving an *unfrozen* (train-mode) Definition raises `UnfrozenDefinitionError` — a training
artifact has no stable identity to key the registry. An unknown name (or a name in another
org — tenancy is enforced) raises `UnknownNameError`.

## `modify` / `reset` — the remaining git verbs

Two free functions compose the store verbs into the git checkout/commit pair.

```python
# modify: recall → fn → save(parent=old_sha). fn composes via the with_* operators,
# each returning a new frozen Definition, so modify just seals the lineage edge.
new_sha = cw.modify(ds, "triage",
                    lambda d: cw.with_skill(d, cw.SkillRef(id="severity", version="0.1")))
assert len(ds.log("triage")) == 3           # the pointer advanced, parent edge recorded

# reset: a pure pointer move (git checkout). Mints no object, no lineage event.
cw.reset(ds, "triage", sha)                 # rewind to the original
assert ds.recall("triage").content_sha() == sha
```

`modify` is **train-mode only** in spirit: it routes through the same `with_*` content-hash
law as composition (no in-place edit, no model call). An `fn` that edits a recalled frozen
Definition in place raises `FrozenError`; one that returns an unfrozen draft raises
`UnfrozenDefinitionError`.

`reset` is a **pure pointer move** — it mints nothing, is reversible, and refuses a `to`
that isn't in `log(name)` (`UnreachableShaError`). It never prunes orphans, so an earlier
sha stays recallable and `craw share` reproducibility holds. (Three-way `merge` is deferred
to the typed-diff work.)

This is the whole git model: a **mutable name pointer** (`save`/`reset` move it) over an
**append-only, immutable, content-addressed object store** (every frozen Definition is its
own sha).

## Summon knowledge — the `Wiki`

A `Wiki` is a versioned, content-hashed, **summonable** knowledge unit. Its `content_sha`
is a **Merkle over page leaves**, so a re-hash only re-derives the page you changed, and two
structurally identical Wikis collapse to one sha.

```python
arch = (
    cw.Wiki(org_id="local")
    .with_page("escalation", {"rule": "P0 → page on-call immediately"},
               trust=cw.TrustTier.TRUSTED)
    .with_page("taxonomy", {"labels": ["bug", "billing", "feature"]},
               trust=cw.TrustTier.TRUSTED)
)
```

`with_page` is copy-on-write — a **new frozen** Wiki with a distinct sha; the receiver is
unchanged, and replacing a title overwrites only that page. Pages are **tainted by default**
and stay tainted across a CoW edit. Every page carries a `TrustTier`
(`TRUSTED` / `COMMUNITY` / `UNTRUSTED`, default untrusted) so a low-trust corpus is never
silently trusted like `repo/src`; the tier only ever **raises** suspicion — it never lowers
taint.

Summon it into a Definition by **pinned snapshot**:

```python
agent = cw.with_context(base, arch, mode=cw.SummonMode.READONLY)
```

`readonly()` returns a `SummonRef` carrying the unit id + **pinned content sha**, never the
body — so `export()["checksum"]` tracks the pin and the page values never appear in the
export payload. `mutable()` is the train-mode edit handle and is **rejected on a frozen
(eval-mode) Wiki**, mirroring `train()`/`eval()`: knowledge edits are copy-on-write only.

When a step needs the knowledge, `consult()` materialises it as `Context` whose entries are
**tainted (fluid)** — so summoned knowledge flows through the fluid-data block and can never
reach an instruction slot or a static-only Sink target. It is a pure `(wiki) -> Context`
with no model call:

```python
ctx = arch.consult()
assert all(entry.tainted for entry in ctx.entries)   # data, never instructions
```

Persistence rides the `Store` seam (`persist()` / `load()`), tenancy-scoped by `org_id`; a
`ScrubbingStore` redacts secrets on the write, so no secret body lands unredacted, and a
Wiki in one org never loads under another.

!!! note "`Rag` is the deferred half"

    The larger retrieval half — `Rag`, retrieval over a content-hashed corpus snapshot —
    ships as a **seam only** (`RagSeam` protocol + `RagDeferred` marker). The seam locks in
    two properties now so the deferred impl can't regress them: embeddings route through the
    secret-scrubbing seam, and retrieved hits are tainted by default and carry the source
    page's trust tier. Calling it today raises `RagDeferred`.

## What stays static (the security spine)

Composing, versioning, and summoning never widen the
[security spine](../architecture/SECURITY.md):

- **Consequential knobs stay static.** Model, policies, and Sink targets are author config —
  `with_*` never derives them from a fluid value.
- **Summoned knowledge is data.** A summon enters identity by its **pinned sha** (the body is
  never carried in the ref or the checksum), and `consult()` entries are **tainted**, so they
  can never reach an instruction slot or a static-only Sink.
- **Acting is eval-only.** `save` requires a frozen Definition; `mutable()` is rejected on a
  frozen Wiki. Only sealed, content-addressed, eval-mode values touch the world — the same
  boundary as the prompt-injection one.

See the [Concepts → agents as variables](concepts.md#the-agents-as-variables-half-compose-version-summon)
section for the mental model, and the
[Definition reference](../reference/definition.md) and
[persistence reference](../reference/persistence.md) for exact signatures.
