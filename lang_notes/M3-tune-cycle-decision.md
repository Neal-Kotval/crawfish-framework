# M3 — Break the definition↔tuner import cycle by extracting light tune types

### D — Move TuneSpec/KnobDomain to a low-dep `crawfish/tune.py`; tuner re-exports  (arch, layering)
**Problem:** `Definition.tune: TuneSpec` forces Pydantic to resolve `TuneSpec` at schema-build time,
which happens at IMPORT (`runtime/base.py:127` `RunRequest.model_rebuild()` completes Definition's
schema). But `TuneSpec` lives in `tuner.py`, behind the cycle `tuner → eval → metrics → batch →
definition.types` (and `batch.py:21` imports `definition.types` before defining `Task`). So importing
`tuner` from `definition.types` (eager or lazy) deadlocks; Pydantic also won't tolerate a deferred
nested model. The whole package fails to import.

**Options:**
- A: extract the LIGHT tune value-types (`KnobValue`, `KnobDomain`, `TuneSpec`, `tune_spec_sha`) into a
  new module `crawfish/tune.py` (deps: pydantic/json/hashlib/tomllib only — no cycle). `definition.types`
  imports `from crawfish.tune import TuneSpec`; `tuner.py` re-exports them (same class objects, so
  `from crawfish.tuner import TuneSpec` and impl-tuner's tests/round-trips are unchanged).
- B: type `Definition.tune` as the serialized `dict` form (drop the type from the schema).
- C: make tuner's eval/metrics imports lazy (can't — `compare`/`is_regression` are used at runtime).

**Decision:** A. It keeps `Definition.tune` strongly TYPED (the design intent), fixes the layering
honestly (value-types belong in a low layer both `definition` and `tuner` can import), and is a ~4-line
move + 1 re-export line that preserves the public API and the SAME class object. B loses type safety;
C isn't feasible.
**One-owner note:** this requires a minimal, API-preserving edit to `tuner.py` (impl-tuner's file) — a
pure extract+re-export, no behavior change. Authorized as a release-manager/architect cycle-break;
impl-tuner's TuneSpec class identity and tests are preserved (same object, re-exported).
**Spine impact:** layering/versioning — the content-hash wiring is unchanged: empty tune is hash-neutral
(`tune` key omitted; demo lock stays `0.1-7113bfa78543`), non-empty tune folds `tune_spec_sha`.
Supersedes the earlier "store tune as dict" suggestion.
