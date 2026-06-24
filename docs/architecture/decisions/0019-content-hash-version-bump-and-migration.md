# ADR 0019 — Content-hash / run-identity schema extension is forward-compatible and fold-only-when-non-default

**Status:** Accepted · **Date:** 2026-06-23 · **Milestone:** F (Agent Language foundations)

> Issues CRA-194 (F-1, cassette key), CRA-195 (F-2, loop ledger), CRA-198 (F-5,
> decode-knob re-freeze). The Agent Language adds new components to three hashed /
> keyed identities at once — the replay cassette key, the loop ledger key, and the
> Definition content hash. Each addition is individually back-compatible; this ADR
> records the *shared* policy that keeps them so, and the migration/hash-bump rules
> for any future change that would move a legacy identity. Numbering: 0018 was the
> last accepted ADR → this is **0019**. Complements ADR 0014 (store-schema migrations),
> ADR 0017 (decode-knob ownership), and ADR 0012 (definitions are versioned).

## Context

Three identities now gain components in Milestone F:

1. **The replay cassette `_key`** (F-1, `runtime/replay.py`) — the *execution coordinate*
   of a run. It folds three new components beyond the legacy core (`id`, `version`,
   `role`, `model`, `inputs`, `session_id`): an `ExecutionCoordinate` (sample / iter /
   visit / depth axes), `org_id`, and `decode_seed`.
2. **The loop/program ledger key** (F-2, `ledger.py`) — a *new* composite key space
   `(loop_id, item_id, edge_id, visit) -> output_ref` (+ a `(loop_id, item_id, depth)`
   recurse variant), persisted under a new `ledger_loop` record kind. `loop_id` is a
   length-prefixed `sha256` over `(v, body_version_sha, item_lineage, edge_id)`, tagged
   with `_LOOP_ID_VERSION`.
3. **The Definition content hash** (F-5, `definition/types.py`) — adds the tunable decode
   knobs `temperature` / `top_p` / `sample_k` to the hashed field set, marked by
   `CONTENT_HASH_VERSION = 1`.

A naive extension of any of these would move existing keys/shas — silently invalidating
every persisted replay cassette, every recorded ledger row, and every frozen artifact's
`version.sha`. That is unacceptable for a local-first framework where a user's
`.crawfish` state predates the upgrade. Each subsystem solved it the same way, and the
shared rule deserves to be recorded once so future additions cannot regress it.

## Decision

**Identity-schema additions are forward-compatible and *fold-only-when-non-default*: a
new component enters the hash/key only when it carries a non-default value, so an
artifact or run that does not use the new component re-derives its pre-F identity
byte-for-byte. A change that *would* move a legacy identity (promoting a now-optional
component to always-on, changing the canonicalization, or adding an always-folded field)
must bump the relevant version constant and ship a migration under ADR 0014.**

### F-1 — cassette key extension, legacy preserved byte-for-byte

- The three new components are appended to the canonical key dict **only when non-default**:
  `coordinate` only when a non-empty `ExecutionCoordinate` is passed (an all-`None`
  coordinate folds nothing), `org_id` only when `!= "local"`, `decode_seed` only when
  present and non-`None`.
- `_key(request, *, org_id="local", coordinate=None)` keeps `request` positional, so every
  existing caller (notably `cache.py`'s `cache_key`) reproduces the legacy key.
- **Guarantee:** with no coordinate, `org_id == "local"`, and no decode field, `_key`
  produces the exact pre-F-1 key. Pinned in `test_replay_key.py` (`LEGACY_KEY =
  "8dd9f4eb30b6b0ed"`). Legacy unsalted cassettes still resolve. `decode_seed` is read
  defensively via `getattr` so F-1 does not couple to F-5's field landing.

### F-2 — loop ledger is a new key space, not a migration of the old one

- The linear pipeline ledger (`checkpoint_step` / `completed_steps`) is **untouched**.
  The loop/recurse coordinates live under a *new* record kind `ledger_loop` in the
  existing generic `records` table — a new namespace, not a schema change to an old one.
- `loop_id` is *derived*, never minted with `new_id()`, so the same loop body over the
  same item along the same back-edge re-derives the same id across processes — and resume
  re-charges \$0 for iterations already recorded. Inputs are length-prefixed and
  version-tagged (`_LOOP_ID_VERSION = 1`) so distinct concatenations cannot collide.
- The migration is **additive**: `store/migrations.py` adds **Migration 3**
  (`CURRENT_SCHEMA_VERSION -> 3`), `CREATE INDEX IF NOT EXISTS idx_records_org_kind ON
  records(org_id, kind)` — idempotent, additive, no existing migration altered (ADR 0014
  authoring contract). It keeps the `completed_visits` / `completed_depths` scans sargable;
  it does not rewrite any row.

### F-5 — decode-knob content-hash bump is hash-neutral when None

- The canonical hash payload is `Definition.content_dict()`, which **drops each decode
  knob that is None** (and excludes the volatile `version` and identity-only `id`).
- **Unmigrated artifact (no knob pinned):** `content_dict()` is byte-identical to the
  pre-v1 payload → `version.sha` **unchanged** → **no re-freeze required**.
- **Artifact that pins any knob:** the knob enters the hash → new sha → must be re-frozen.
  Correct: pinning a decode knob makes it a different artifact.
- `CONTENT_HASH_VERSION` is introduced at **1** to mark the hashed-field-set change. (See
  ADR 0017 for the ownership rationale; this ADR records only the hash-neutrality /
  migration property.)

### Output content hash (F-0) — the shared output identity

`crawfish.output.output_content_sha` is the canonical content hash of a frozen `Output`
(SHA-256 over canonical JSON of `output_schema`, `value`, `produced_by`, `lineage`,
`tainted`; `id` is excluded so structurally-equal Outputs hash equal). It is folded with
a `_CONTENT_SHA_VERSION` (currently `1`). It is the `output_ref` recorded by the F-2 loop
ledger. The same bump rule applies: any change to the hashed field set or canonicalization
**must** bump `_CONTENT_SHA_VERSION`, which re-keys any ledger persisted on it.

### The migration / hash-bump policy (the load-bearing rule)

Until this ADR is superseded:

- **No component may be made unconditional.** A component that is "fold-only-when-non-default"
  today may not be promoted to always-folded without a version bump + migration, because
  that would move every legacy key that lacked it.
- **Every version constant is a migration trigger.** Bumping `_CONTENT_SHA_VERSION`
  (F-0 output hash), the cassette-key schema (F-1), `_LOOP_ID_VERSION` (F-2), or
  `CONTENT_HASH_VERSION` (F-5) re-derives the affected identities and therefore requires
  a re-key/migration of any state persisted on the old value, authored per ADR 0014.
- **The pipeline ledger is sacrosanct.** F-2 added a parallel key space; it did not and
  may not change `ledger_pipeline` / `ledger_item` / `ledger_run`.

## Alternatives rejected

- **Emit the new components unconditionally (e.g. `"org_id": "local"`, `"temperature":
  null`) and accept the global churn.** Rejected: a one-line default would silently
  invalidate every frozen artifact and every replay cassette on disk. The
  fold-only-when-non-default rule is precisely what avoids that.
- **A bulk rewrite migration that re-keys all existing rows to the new schema.** Rejected:
  unnecessary because the new components are absent on legacy data by construction, and
  expensive/risky on a user's local `.crawfish` state. Lazy, additive, byte-compatible
  beats eager rewrite.
- **Reuse the linear pipeline ledger for loop/recurse progress.** Rejected (F-2): a
  `step_index: int` cannot represent per-`(item, edge, visit)` progress or a per-item
  recursion-depth stack. A new key space is correct; mutating the old one would break
  every existing pipeline resume.
- **Mint `loop_id` with `new_id()`.** Rejected: a random id is not reproducible across
  processes, so resume could not match already-recorded iterations and would re-charge for
  completed work. Loop identity must be derived.

## Consequences

- A user upgrading into Milestone F keeps every existing replay cassette, ledger row, and
  frozen artifact valid — nothing on disk moves. New capability (coordinates, tenancy in
  the key, decode knobs, loop resume) is opt-in by use.
- `org_id` now enters both the cassette key (F-1) and every loop-ledger row (F-2), so
  cross-tenant identities can no longer collide — recorded in [`SECURITY.md`](../SECURITY.md).
- The four version constants (`_CONTENT_SHA_VERSION`, the cassette-key schema,
  `_LOOP_ID_VERSION`, `CONTENT_HASH_VERSION`) are the explicit migration triggers; the next
  change to any hashed/keyed identity ships its bump and migration together under ADR 0014.
- The cassette `_key` is now the documented, versioned **execution-coordinate** contract
  that every leaf-re-running operator (quorum, Refine, MCTS, recurse) must stamp — recorded
  in [`ARCHITECTURE.md`](../ARCHITECTURE.md).
