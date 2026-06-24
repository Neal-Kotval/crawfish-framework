# CRA-209 (AL-T1) — wire `Definition.tune` into the content hash

Completes the AL-T1 acceptance clause the tuner owner could not (the tuner does not own the
`definition/` package): folds a `TuneSpec` through `Definition` so the tunable knob space —
Axis 1 as *data* — round-trips through `export()` and versions the agent via the content sha,
**hash-neutral when empty**.

## Import-cycle resolution: the light `crawfish.tune` module

`Definition.tune` annotates `TuneSpec`, so `crawfish.definition.types` must resolve that type at
schema-build time (forced at import by `runtime/base.py`'s `RunRequest.model_rebuild()`). But
`crawfish.tuner` sits behind a hard cycle — `tuner → eval → metrics → batch → definition.types`,
and `batch.py` imports `definition.types` *before* defining `Task` — so importing `tuner` from
`definition.types` (at any point) deadlocks. Pydantic also does not tolerate a deferred/incomplete
nested model.

The light tune types are therefore split into a new **`crawfish/tune.py`** module with **no
crawfish imports** (only `pydantic`/`json`/`hashlib`/`tomllib`): `KnobValue`, `KnobDomain`,
`TuneSpec`, `tune_spec_sha`. `definition.types` imports `from crawfish.tune import TuneSpec`
(cycle-free), and **`crawfish.tuner` re-exports the same class objects** so
`from crawfish.tuner import TuneSpec` keeps working and stays *identity-stable* — a
`tuner.TuneSpec` instance is accepted by `Definition.tune` because it is the same class. (Without
the re-export there would be two distinct `TuneSpec` classes and
`Definition(tune=tuner.TuneSpec(...))` would raise a pydantic `ValidationError`.)

## Public API added

**`crawfish.definition.types`**
- `Definition.tune: TuneSpec | None = None` — the tunable knob space (authored as `tune.toml`).
  `None`/empty → tune-less. Folded into the content identity via `content_dict()` (below).

**`crawfish.definition.compiler`**
- `load_definition` now discovers `tune.toml` (array-of-tables of `[[knob]]`) in the Definition
  directory and populates `Definition.tune`. An absent or **empty** `tune.toml` leaves the
  Definition tune-less (`tune=None`) — it never authors an empty `TuneSpec`, so it stays
  hash-neutral.

## The hashing rule (empty vs non-empty tune)

`Definition.content_dict()` folds the tune-spec into the canonical hash payload as follows:

- **Tune-less (`tune is None` or `tune.knobs == []`)** → the `tune` key is **omitted entirely**
  from `content_dict()`. The payload is byte-identical to the pre-change payload, so the content
  sha (and the directory-derived `version.sha`) is unchanged. Adding an *empty* `tune.toml` is a
  no-op on identity.
- **Non-empty tune** → `content_dict()["tune"] = tune_spec_sha(self.tune)`. Editing the search
  space changes the sha — exactly like editing any other knob. Tuning *is* a content change, so
  it versions the agent.

The tune-spec is folded as its **sha** (not its raw dict) so the payload stays compact and the
rule is one line. We **omit** rather than fold-the-constant when empty because `tune_spec_sha`
of an empty spec is a non-trivial constant (the sha of `{"knobs": []}`); folding that constant
would perturb every pre-existing tune-less artifact's hash. Omission is what makes the change
hash-neutral.

## `CONTENT_HASH_VERSION` — deliberately NOT bumped

`CONTENT_HASH_VERSION` stays at `1`. The hashed field-set *did* gain an optional field, but a
tune-less artifact omits it from `content_dict()` and keeps its pre-change sha byte-for-byte — so
bumping the constant would needlessly re-key every existing frozen artifact for no content change.
This mirrors the F-5 decode-knob precedent: hash-neutral additions do not bump the version. Only a
non-empty tune diverges the sha (and that artifact must be re-frozen — correct and desired).

## Round-trip through `export()`

`Definition.export()` dumps `model_dump(mode="json")`, which serializes the nested `TuneSpec` to a
plain dict (`{"knobs": [...]}`) for tune-bearing Definitions and `null` for tune-less ones. Pydantic
reloads either form back into `Definition.tune` when the marketplace `definition` payload is
re-instantiated, so a `TuneSpec` round-trips losslessly.

## Verification

- Demo lock (`demo/triage-bot/definition.lock`) regenerates **unchanged** at
  `0.1-7113bfa78543` — the demo authors no `tune.toml`, so it is tune-less and hash-neutral.
- `test_definition_tune.py`: tune-less sha == pre-change sha; a non-empty tune changes the sha and
  round-trips through `export()`; a `tune.toml`-authored spec lands in `Definition.tune`.
- Full suite green (definition hashing is load-bearing; every sha-snapshot test stays green).
