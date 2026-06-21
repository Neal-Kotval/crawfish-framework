# Phase 2 build log (CRA-170)

Orchestrator-maintained running log of what shipped, decisions, and open items for the
Phase 2 epic. Per-issue detail lives in the Linear issues; this is the merge/ordering record.

## Conventions

- Build in dependency order (pinned comments on CRA-170). **CRA-184 contracts merge first, alone.**
- Per-issue gate: `ruff` + `ruff format --check` + `mypy --strict` + `pytest -q` green & deterministic;
  security spine upheld; a demo exercises it; docs + ADR updated; all VETO reviewers pass.
- One owner per hot file (`run.py`, `output.py`, `runtime/base.py`, `cost.py`, `secrets.py`,
  `metrics.py`, `cli.py`). Serialize contended files.

## File-ownership map (from CRA-170 review comments)

| Hot file | Owner order |
| -- | -- |
| `run.py` | CRA-171 → CRA-172 (serialize); CRA-174 rebases after |
| `output.py` | CRA-172 owns; CRA-174 rebases after |
| `runtime/base.py` | CRA-184 (contracts) → CRA-173 |
| `cost.py` | `resolve_model` extracted in CRA-184 → CRA-182 builds on it |
| `metrics.py` | CRA-172 (removes hack) → CRA-175 expands |
| `secrets.py` | CRA-178 → CRA-180 |
| `cli.py` | CRA-180 / CRA-181 (distinct subcommands; coordinate) |

## Status

### ✅ CRA-184 — Interface freeze (contracts) — phase-2a — DONE (pending merge)
Branch: `nealkotval/cra-184-...`. Types + stubs + tests only, no behavior.

Shipped:
- `emission.py` — frozen `Emission` + closed `EmissionKind` (10 kinds) + `REQUIRED_ATTRS`
  (frozen MappingProxy) + `EMISSION_SCHEMA_VERSION`. `missing_attrs`/`is_valid` real;
  `to_event`/`from_event` stubs (CRA-171). `tainted` propagation field present.
- `validation.py` — `ValidationFailure` enum, frozen `ValidationError`, frozen `StructuralDiff`
  (`.equal`); `validate_output` / `validate_inputs` / `structural_diff` stubs (CRA-172).
- `provider.py` — `resolve_model` (single shared resolver, **real**), `Provider` protocol
  (runtime_checkable), frozen `ModelsConfig` + `ProviderPolicy`. `cost._resolve_model` and
  `CommandRuntime._resolve_model` now delegate to it (behaviour-identical; de-duplicated).
- `secrets.py` — frozen `Grant` dataclass (consumed by CRA-178/180).
- `__init__.py` — all symbols exported + in `__all__`.
- Tests: `tests/test_interface_freeze.py` (18 tests) — taxonomy closure, frozenness, stub
  honesty, resolver parity with legacy call sites, protocol structural check.
- Docs: ADR 0013 (taxonomy / inline Output.value / single resolve_model);
  `docs/architecture/emission-taxonomy.md`.

DoD gate: ruff clean · ruff format clean · mypy strict clean (70 files) · pytest 374 passed.

Decisions (ADR 0013): EmissionKind closed+versioned; `Output.value` inline (ArtifactRef
opt-in, single deref point); one `resolve_model` (no hardcoded vendor default in provider.py).

### ✅ CRA-171 — Typed emission substrate — phase-2a — DONE (pending merge)
Branch: `nealkotval/cra-171-...`. Implements the behavioural half of the CRA-184 emission contract.

Shipped:
- `emission.py` — `to_event`/`from_event` (typed + legacy back-compat shim, never raises),
  `emit` (per-run volume cap; drop-only, rotation deferred to CRA-191), `read_emissions`.
- Routed all existing telemetry through `Emission`: `runtime/base._emit_telemetry` → MODEL,
  `run.py` → RUN_START/RUN_FINISH (taint from `Output.tainted`), `nodes/sink` → SINK,
  `runtime/context_strategy` → COMPACTION, `observe` → OBSERVER.
- `inspector.py` reads the typed stream via `read_emissions`.
- **Cost-consumer regression fixed** (bug-reviewer BLOCKER): the new emission shape
  (`kind="model"`/`"run_finish"`, cost under `attrs`) broke `cost.spent_today`/`_event_cost`.
  Fixed: `_event_cost` reads nested `attrs`; `_COST_BEARING_KINDS` accepts typed+legacy kinds;
  new `_parse_event_ts` handles ISO-string AND epoch-float ts. Tests rewritten to be load-bearing.
- `emit`/`read_emissions` exported. Demo: `test_demo_run_produces_typed_emission_stream` runs
  triage-bot on MockRuntime; secret-never-in-ledger test through ScrubbingStore.

DoD gate: ruff clean · format clean · mypy strict clean (70 files) · pytest 390 passed.
Reviews: Security & Architecture **APPROVE**; Bug **APPROVE** (after cost fix re-review).

Non-blocking follow-ups (→ CRA-191): `from_event` typed/legacy disambiguation is implicit
(known-kind + `attrs` dict) — not reachable from real producers but a latent trap; `emit`'s
`max_per_run` guard is wired but unused by emit sites (O(n)/emit when enabled — revisit at rotation).
Follow-up (→ emit sites): stamp real `ts` on MODEL/RUN_FINISH so per-day cost filtering is exact
(currently `ts=0.0` ⇒ always counted, never undercounts).

### ✅ CRA-172 — Typed input & output validation — phase-2a — DONE (pending merge)
Branch: `nealkotval/cra-172-...`. Implements the frozen `validate_output`/`validate_inputs`/`structural_diff`.

Shipped:
- `validation.py` — registry-driven validator (NO new dep): tolerant parse-from-text (code-fence
  strip + outermost balanced `{...}`/`[...]`, escape-aware), TypeDef walk (PRIMITIVE/RECORD/LIST/
  OPTIONAL), `canonicalize()` (sorted keys → deterministic equality/diffs). New `ValidationAction`
  enum (RETRY/REPAIR/DEAD_LETTER) — distinct from the frozen `ValidationFailure` reasons.
- `run.py` — typed input validation BEFORE any model call (`InputValidationError`); typed
  `Output.value` (not string); `ValidationAction` policy; REPAIR re-prompt is metered + now has a
  **pre-flight budget guard** (skips the extra call when `cost_budget.remaining_usd<=0` → dead-letter);
  tool-result run forces `tainted=True` (injection vector) even with all-static inputs.
- `metrics.py` — typed values read directly (JSON-decode hack demoted to no-schema fallback).
- `nodes/router.py` — classifier opts out of input-type/output-schema validation (over-binds free text).
- demo/triage-bot gains a typed `Triage` RECORD output (end-to-end MockRuntime assert: value is a dict).

DoD gate: ruff clean · format clean · mypy strict clean (70 files) · pytest 414 passed.
Reviews: Bug **APPROVE**, Security & Architecture **APPROVE** (full 7-point spine pass).
Back-compat: no-schema Definitions keep `Output.value` as the raw string; all `.value` consumers
(aggregator/eval/team/sink/source) verified dict-safe.

Non-blocking follow-ups (→ CRA-175 evals/golden-set): `validate_output` keeps only the FIRST
top-level JSON object when a model emits several (best-effort, no signal); `ValidationFailure.EMPTY_SCHEMA`
is a reserved-but-unemitted reason. Golden-set string→typed migration helper to be owned by CRA-175.

## Review-surfaced notes for downstream issues
- **CRA-185** (taint-conformance suite): add explicit acceptance criterion — `tool`/MCP-result
  emissions MUST be `tainted=True`. The Emission envelope *carries* taint; producers enforce it.
  (Security reviewer, CRA-184 gauntlet.)
- **CRA-192** (model aliases): reject alias→alias chains at config-load (`resolve_model` is
  single-hop by contract; a 2-hop alias yields a non-concrete id that fails at runtime).
- **CRA-173** (provider/failover): when failover lands, alias-expand *all* entries of a model
  list, not just the primary `model[0]`.

## Open items / deferrals
- Audit-log tamper-evidence (hash-chain) and cross-org data governance: noted on CRA-171/173
  as "decide or defer" — not blocking Phase 2; conscious deferral.
