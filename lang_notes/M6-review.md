# M6 Review — Variables & Knowledge (CRA-223..227)

Branch: `cra/m-6` (commits `3f365e8`, `fc2250f`). Combined ARCH + SECURITY review.

- **ARCH verdict: PASS-WITH-NOTE** (one non-blocking convergence item: duplicate `SummonRef` / `SummonMode` across `derive.py` and `wiki.py`).
- **SECURITY verdict: PASS.**
- **BLOCK defects: none.**

Scope reviewed: `derive.py`, `wiki.py`, `definition_store.py`, `tuner.py` (re-export), `learning.py` (import), and the three test files. Spine: `CLAUDE.md`, `SECURITY.md`, `definition/types.py` (`content_dict`/`content_sha`/`export`).

---

## DV-0 (CRA-223) — shared content-hash path

**Invariant: one hash law, no second path. CONFIRMED.**

- `derive.py` is low-dep: imports only `pydantic`, `core.types`, `definition.types`, `versioning.version`. **No** eval/metrics/batch/runtime import, so `definition.types` could import it without a cycle and `import crawfish.tuner` stays cheap. No import cycle.
- `content_sha` delegates to the canonical `Definition.content_sha()` (`derive.py:70`) — it does **not** re-implement the law. `refreeze` stamps `content_sha(mutated)` onto a fresh `Version` and seals (`derive.py:83-87`).
- `tuner.py:107` imports `_refreeze, _with_agents` from `crawfish.derive`; `derive.py:103-105` aliases `_content_sha/_refreeze/_with_agents` to the public objects. `test_derive.py:27-35` pins object identity (`tuner._refreeze is derive.refreeze`, `derive._content_sha is derive.content_sha`). Same helper objects, byte-identical hashing. **Re-export confirmed.**
- `learning.py` import fix is benign (it composes the Tuner; no new hash path).

## AL-DV1 (CRA-224) — copy-on-write composition

**Invariant: every `with_*` → new FROZEN Definition, receiver untouched, idempotent sha. CONFIRMED.**

- `with_agent/with_skill/with_context/with_inputs/with_policy` all do `model_copy(deep=True)` → structural edit → `refreeze` (`derive.py:178-245`). Receiver never mutated; result frozen with a fresh `version.sha`. `test_derive.py:51-59` (base unchanged), `:70-78` (idempotent same-knobs / divergent diff), `:91-97` (mutating the *result* raises `FrozenError`).
- **Reference-not-embed for pins. CONFIRMED.** `with_skill` folds `DefinitionRef(id="skill:<id>", version=...)` into `dependencies` (`derive.py:200-204`); `with_context` folds `DefinitionRef(id="summon:<id>:<mode>", version=str(obj.version))` (`derive.py:218`). No mutable body is copied inline — only the `{id, version}` pin. The summon version is **snapshotted at compose time** (`str(obj.version)`), so a moving pointer is `recall`, not this. Checksum tracking verified against the spine: `content_dict` (`definition/types.py:203-227`) starts from `model_dump` and pops only `version`/`id`/empty-decode/`tune`, so `dependencies` remains in the payload — therefore the skill/summon pin folds into both `content_sha` and `export().checksum`. `test_derive.py:99-128` confirms checksum moves iff the pin moves.
- **Consequential knobs stay static, never fluid-derived. CONFIRMED.** `with_policy` documents and treats a `Policy` as static consequential config (`derive.py:237-245`); `with_inputs` carries each `Parameter`'s static/fluid taint through unchanged and never widens fluidity (`derive.py:225-234`). No operator derives a model/policy/Sink target from a fluid input.

## AL-DV2 (CRA-225) — save / recall (`DefinitionStore`)

**Invariants CONFIRMED.**

- **save requires FROZEN (un-versioned mutation rejected).** `save` raises `UnfrozenDefinitionError` unless `definition.frozen` (`definition_store.py:231-235`). `test_definition_store.py:49-55`.
- **name pointer is the sole mutable plane; versions append-only.** Three kinds: `definition_object` (content-addressed, dedup), `definition_name` (the one mutable row — `_move_pointer`, `:169-171`), `definition_version` (append-only, deterministic event id keyed on `(org,name,seq)`, `:174-190`). Byte-identical content dedups the object but records two pointer events (`test:73-84`).
- **Write order is safe:** object + lineage event are written **before** the pointer moves (`save`, `:236-249`) — a reader never sees a pointer to an absent object.
- **recall is data-only and never mints a sha.** Reads a stored object and re-seals frozen (`_get_object` `:147-159`; `recall` `:252-287`). `test:101-110`. Latest / `name@sha` / bare-sha all resolve by read only.
- **Org isolation. CONFIRMED.** Every `Store` call passes `org_id=self.org_id`; `recall`/`log` raise `UnknownNameError` cross-tenant (`test:112-121`). No cross-org recall.

## AL-DV3 (CRA-226) — modify / reset

**Invariants CONFIRMED.**

- `modify` = `recall → fn → save(parent=old_sha)` (`:303-332`), atomic via `save`, with a lineage edge (`parent_sha`). `test:139-151`.
- **modify is train-mode only.** A recalled Definition is frozen; an `fn` that edits in place raises `FrozenError` (`test_modify_on_eval_mode_in_place_edit_raises`, `:154-163`). `modify` also rejects an unfrozen `fn` result via `UnfrozenDefinitionError` (`:327-331`).
- **reset is a pure pointer move (git checkout).** Mints no object/event, refuses a sha not in `log(name)` (`UnreachableShaError`, `:335-352`). `test:185-207`.
- **Deterministic:** same start + pure `fn` ⇒ same resulting sha across independent stores (`test:172-182`).

## AL-DV4 (CRA-227) — Wiki / Rag seam

**Invariants CONFIRMED.**

- **Freezable + Summonable.** `Wiki(Freezable)`; `content_sha` is a Merkle over `WikiPage.page_sha` leaves, excludes `id`/`org_id` (identity not body) (`wiki.py:168-178`). `with_page` is CoW → new frozen Wiki, receiver unchanged, distinct sha (`:194-228`; `test:26-43`). Mutating a frozen Wiki raises (`test:61-65`).
- **Summon by pinned version; body never embedded.** `readonly()` → `SummonRef` pinned at `str(version)`; `export()` carries the pinned sha + Merkle leaves (title/sha/trust) but **no page value** (`:277-295`). `test_export_carries_pinned_sha_not_body:130-147` asserts the secret body is absent from the export blob.
- **consult() reaches the model as DATA / tainted, never an instruction slot or static Sink.** Pages are `tainted=True` by default; `consult` materialises a `Context` of tainted `ContextEntry`s (`:261-274`; `test:67-86`). This is the SECURITY.md fluid boundary — summoned knowledge is data, never instructions.
- **TrustTier never LOWERS taint.** Even a `TRUSTED` page is summoned tainted (`test_trust_tier_is_carried_and_never_lowers_taint:88-94`); the tier only raises suspicion (gap S6, stored-injection-via-retrieval).
- **`mutable()` rejected in eval mode** (frozen Wiki) — mirrors `train()`/`eval()` (`:241-254`; `test:120-127`).
- **Org-isolated + secret-scrubbed.** `persist`/`load` scope by `org_id` (`test:161-168`); body routes through the `Store` seam so `ScrubbingStore` redacts secrets on write (`test:171-182`).
- **Rag deferred — no impl.** `RagSeam` protocol + `RagDeferred` marker only; `retrieve` raises (`test:185-203`). Security properties (taint-by-default, trust-tier, embedding-through-scrubbing-seam) are locked in the seam docstring so the deferred impl can't regress them.

---

## Non-blocking convergence item (ARCH note)

**Duplicate `SummonRef` / `SummonMode`.** `derive.py:135-149` defines a `SummonRef{id, version, mode}` (for `with_context` pins) and `wiki.py:130-149` defines a *different* `SummonRef{unit_id, kind, version, readonly}` (for `Wiki.readonly()`). Both are exported in their modules' `__all__`. They are not interchangeable (different field names) and a caller importing the wrong one will mis-bind. This is a naming collision, not a correctness defect — `with_context` accepts any `Summonable` (structural, `derive.py:152-167`), and `Wiki` satisfies that protocol via its `id`/`version`. 

Suggested convergence (follow-on, non-blocking): unify on one `SummonRef` (likely the richer `wiki.py` shape carrying `kind`/`readonly`) and have `derive.with_context` fold *that* ref, or rename one to disambiguate (e.g. `derive.SummonPin`). File as a convergence task; does not block M6.

## Spine check

`CLAUDE.md` / `SECURITY.md` upheld: FLUID inputs stay data (Wiki taint-by-default + never-lowering trust tier); consequential targets (policies, Sink) stay static, never fluid-derived (`with_policy`/`with_inputs`); idempotency/name pointers key on static, content-addressed shas; secrets route through the `Store` (ScrubbingStore) seam, never embedded in a summon/export; every `Store` row is `org_id`-scoped. No SDK import in nodes; no raw SQL outside the `Store`. Type compatibility for `Summonable` is structural (ADR 0002), not `isinstance`.

Tests: deterministic, no live model calls (all fixtures/in-memory `SqliteStore`). Demo `definition.lock` **unchanged** in this range (no diff `cra/m-5..cra/m-6 -- demo/`).
