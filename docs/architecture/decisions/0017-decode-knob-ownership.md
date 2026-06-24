# ADR 0017 — Decode-knob ownership: tunable knobs on the Definition, per-call knobs on the RunRequest

**Status:** Accepted · **Date:** 2026-06-23 · **Milestone:** F (Agent Language foundations) · **Issue:** CRA-198 (F-5)

> Prerequisite for AL-T1 / AL-T2 / TS-8. Two epic issues independently claimed ownership
> of `temperature`: AL-T1 wanted it (plus `sample_k`) as a first-class tunable `AgentSpec`
> field *inside* the content hash; TS-8 put `temperature`/`decode_seed` on `RunRequest` and
> argued *against* hashing `grammar`. Left unsettled, two subsystems each believe they own
> the same decode parameter. This ADR settles it in code and contract.

## Context

A decode parameter influences model output, so it must be accounted for in **run identity**
(otherwise replay/caching is unsound and the Tuner cannot reason about what it changed). But
the *kinds* of decode parameter differ:

- **Tunable knobs** — `temperature`, `top_p`, `sample_k`. These are exactly what the Tuner
  searches and what `state_dict` serializes. They are part of *what the agent is*, so they
  belong to the **Definition** and must enter its **content hash** (`version.sha`): changing
  one produces a new, distinctly-addressable artifact.
- **Per-call knobs** — `grammar` (a constrained-decode grammar, expressed in provider-specific
  dialects) and `decode_seed` (a per-invocation seed). These are *not* part of the agent's
  identity. Hashing `grammar` would bake a provider dialect into the cross-provider content
  hash and break graceful degradation (TS-8). The `decode_seed` varies per call by design.

The failure mode to prevent: `temperature` appearing as an independently-writable field in
**both** the Definition and the `RunRequest`, where the two can silently disagree — the hash
says one thing, the call does another.

## Decision

**`temperature`/`top_p`/`sample_k` live in exactly one authoritative location — the
`AgentSpec` on the Definition — and enter the content hash. `RunRequest.temperature` is a
read-only property *derived* from the resolved Definition; it is not a settable field.
`grammar` and `decode_seed` are per-call `RunRequest` fields kept out of the content hash;
`decode_seed` enters run identity via the F-1 replay cassette key instead. The `AgentRuntime`
contract advertises a `DeterminismTier` capability so model stochasticity is never conflated
with infra-nondeterminism.**

### One authoritative location for the tunable knobs

- The knobs are optional fields on `AgentSpec` (`temperature: float | None`, `top_p`,
  `sample_k: int | None`). `AgentSpec.decode_knobs()` returns the non-None subset.
- `Definition.resolved_decode(role)` resolves the knobs for the turn's agent (explicit role →
  lead → first agent). This is the **single** read path at run time.
- `RunRequest.temperature` is a `@property` that delegates to
  `request.definition.resolved_decode(request.role)`. Because it is read-only, no caller can
  pin a conflicting value on the request that drifts from the content-hashed Definition value.
  *Acceptance:* "temperature appears in exactly one authoritative location; the other is
  derived" — enforced by `temperature not in RunRequest.model_fields` and by the spec value
  always flowing through.

### `grammar` out of the content hash

`grammar` is a provider dialect (e.g. a JSON-schema grammar for one backend, a GBNF string
for another) and must **degrade gracefully** on a backend that lacks constrained decode. Baking
it into the cross-provider content hash would (a) fork the artifact identity on a purely
presentational provider detail and (b) couple the hash to a backend. It is therefore a per-call
`RunRequest.grammar` field, never in `content_dict()`.

### Every decode field still enters run identity

- Knob path → `version.sha` (the content hash): a changed knob is a new artifact.
- Per-call seed → the **F-1 replay cassette `_key`**: F-5 only guarantees the field exists
  (`RunRequest.decode_seed`, default `None`) and F-1 reads it (via `getattr`) and folds it into
  the cassette key. The seed never enters the Definition hash.

### Determinism capability tier (the runtime contract)

`DeterminismTier((str, Enum))` with `HONORS_SEED` / `BEST_EFFORT` / `NONE`. `AgentRuntime`
carries `determinism_tier: DeterminismTier = BEST_EFFORT`. A backend that bit-reproduces from a
seed overrides to `HONORS_SEED`; a fully stochastic one to `NONE`. `cw.calibrate()` records the
tier so a `BEST_EFFORT`/`NONE` backend's residual variance is attributed to **infra** (a
variance floor), not mistaken for Definition-level stochasticity. The default keeps every
existing runtime valid with no code change.

## Migration / re-freeze

Adding `temperature`/`top_p`/`sample_k` to `AgentSpec` would, under a naive `model_dump`, emit
`"temperature": null` (etc.) into every Definition payload and perturb the sha of **every**
pre-existing frozen artifact. To avoid a forced global re-freeze, the canonical hash payload is
`Definition.content_dict()`, which **drops each decode knob that is None** (and excludes the
volatile `version` and the identity-only `id`). Consequences:

- **Unmigrated artifact (no knob set):** `content_dict()` is byte-identical to the pre-v1
  payload → **sha unchanged**. No re-freeze required.
- **Artifact that pins any knob:** the knob enters the hash → **new sha** → must be re-frozen.
  This is correct: pinning a decode knob makes it a different artifact.

`CONTENT_HASH_VERSION` is introduced and set to **1** to mark the hashed-field-set change
(decode knobs added, hash-neutral when None). `Definition.content_sha()` is the canonical hash
function (`sha256` of the canonical-JSON `content_dict()`, 12 chars). Verified: no existing test
pins a literal sha, and the full suite (736 passed) is green — unmigrated artifacts do not move.

## Alternatives rejected

- **Both subsystems own `temperature`** (the original AL-T1 + TS-8 collision) — a settable
  `RunRequest.temperature` *and* a hashed `AgentSpec.temperature`. Rejected: the two can
  silently disagree; the hash would no longer describe the actual decode. The whole point of
  F-5 is to forbid this.
- **`RunRequest` owns the tunable knobs; Definition references them.** Rejected: the Tuner
  searches Definitions and `state_dict` serializes them; a knob the Definition cannot see is a
  knob the Tuner cannot tune or reproduce. Tunable knobs must be in the content hash.
- **Hash `grammar` into the content hash.** Rejected: couples the cross-provider artifact
  identity to a provider dialect and breaks graceful degradation (TS-8).
- **Emit the knobs as `null` and accept the global sha churn.** Rejected: a one-line decode
  default would silently invalidate every frozen artifact and every replay cassette. The
  hash-neutral-when-None payload avoids it.
- **Per-instance determinism flag instead of a tier enum.** Rejected: a typed three-value tier
  is what `cw.calibrate()` needs to attribute a variance floor; a bool cannot distinguish
  "best-effort" from "ignores the seed".

## Consequences

- `temperature` has one source of truth; `RunRequest` reads it derived. Replay and the Tuner
  agree on what a Definition's decode is.
- Adding decode knobs did **not** re-freeze existing artifacts (hash-neutral when None); pinning
  a knob deliberately mints a new artifact.
- F-1 owns the cassette-key fold of `decode_seed`; F-5 guarantees the field exists. `cw.calibrate`
  (downstream) reads `determinism_tier` to separate infra variance from model variance.
- `docs/architecture/ARCHITECTURE.md` (determinism-tier in the runtime contract) and
  `docs/architecture/SECURITY.md` (decode fields in run identity) get the follow-up notes in the
  F-5 changelog.
