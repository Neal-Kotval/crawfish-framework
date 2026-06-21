# ADR 0013 — Emission taxonomy, inline `Output.value`, and a single `resolve_model`

- Status: Accepted
- Date: 2026-06-21
- Context: Phase 2 (CRA-170) interface freeze (CRA-184)
- Supersedes: none. Refines ADR 0005 (universal model type), ADR 0008 (observer surface).

## Context

Phase 2 fans out into ~22 parallel streams that all code against three contracts that
did not yet exist: the typed `Emission` (CRA-171), the typed `Output` value + validators
(CRA-172), and the `Provider` protocol + a shared model resolver (CRA-173/CRA-192).
Landing those contracts inside their full implementation PRs would churn every downstream
issue each time a signature moved, and CLAUDE.md's one-owner-per-file rule means the hot
files (`run.py`, `output.py`, `runtime/base.py`, `cost.py`) cannot be edited by five
streams at once. CRA-184 freezes the contracts first. Three decisions had to be settled
here so downstream issues do not each guess.

## Decision

### 1. The `Emission` taxonomy is closed and versioned

There is **one** typed signal — `crawfish.emission.Emission` — that every producer emits
onto the append-only ledger and every consumer reads. `EmissionKind` is a **closed** enum
of ten kinds (`run_start`, `run_finish`, `model`, `tool`, `sink`, `compaction`, `observer`,
`metric`, `secret_lease`, `jail_violation`). Each kind declares its required `attrs` keys in
the frozen `REQUIRED_ATTRS` map (the canonical schema, mirrored in
`docs/architecture/emission-taxonomy.md`). `Emission.schema_version`
(`EMISSION_SCHEMA_VERSION = 1`) lets the Store migration (CRA-191) and the dashboard
survive future kind/attr evolution.

Adding a kind or changing a kind's required attrs is a **contract change**: bump
`EMISSION_SCHEMA_VERSION` and extend `REQUIRED_ATTRS` in the same PR.

**Security:** `Emission.tainted` propagates the fluid/untrusted marker across the emission
boundary — one of the three new boundaries (Emission, Context artifact, jail) the
taint-conformance suite (CRA-185) asserts. Tool/MCP results are untrusted; a `tool`
emission carrying their content is tainted.

### 2. `Output.value` is inline by default; `ArtifactRef` is an explicit opt-in

The typed value lives **inline** in `Output.value` (CRA-172 parses and validates it there).
Large blobs use the existing `crawfish.artifacts.ArtifactRef` as an explicit opt-in,
dereferenced at a **single** point against the `ArtifactStore` seam — never implicitly. This
resolves the CRA-174 conflict (transferable Context assumed inline) in favour of inline: #2,
#4, #5, and Sink all assume an inline value, and an always-`ArtifactRef` design would force
a dereference on every read. Validators (`validate_output`, `structural_diff`) operate on the
inline value.

### 3. One shared `resolve_model`

Model-field→id resolution was duplicated in `CommandRuntime._resolve_model` and
`cost._resolve_model`, so the cost preview could silently drift from what the runtime ran.
`crawfish.provider.resolve_model(model, *, default, config)` is now the **single** resolver;
both call sites delegate to it (behaviour-identical: `str`→itself, `list`→first entry,
`None`/`[]`→fallback, with single-hop alias expansion via `ModelsConfig`). No vendor default
is hardcoded in `provider.py` (ADR 0005): callers pass their own `default`; `CommandRuntime`
still owns `DEFAULT_MODEL`, and CRA-192 moves it into `ModelsConfig`. The `ProviderPolicy`
allowed-provider type gates failover (CRA-173) and is consented at install (CRA-180).

## Consequences

- Downstream issues import stable symbols from `crawfish` and compile against them; the
  behavioural halves (`Emission.to_event`/`from_event`, `validate_output`, `validate_inputs`,
  `structural_diff`, the `Provider` implementations) are honest `NotImplementedError` stubs
  with an owning issue named in the message.
- `resolve_model` is the one place routing logic changes (CRA-182 builds the router on it);
  the cost estimate can no longer drift from the runtime.
- The freeze is types/stubs only — no behavioural change merged in CRA-184 — so it can land
  first as one small PR ahead of the fan-out.

## Rejected alternatives

- **Contracts inside each implementation PR.** Rejected: constant rebasing across ~10
  dependents and concurrent edits to one-owner hot files.
- **`Output.value` always an `ArtifactRef`.** Rejected: forces a dereference on every read
  and breaks the inline assumption in #2/#4/#5/Sink.
- **Leave the two `_resolve_model` copies.** Rejected: the cost preview drifts from the
  runtime the moment routing becomes dynamic (CRA-182).
- **Open/extensible `EmissionKind`.** Rejected: an open taxonomy defeats the dashboard's
  "render any property without bespoke code" goal and the migration story; kinds are a
  deliberate, reviewed contract.
