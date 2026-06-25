# ADR 0010 — Jailed compile of agent-authored code

**Status:** Accepted · **Date:** 2026-06-25
**Affects:** the directory compiler, the security spine, `craw code` (CRA-266 / CRA-267),
the provenance ledger
**Relates to:** ADR 0016 (the `Jail` abstraction), RFC 0001 §12 (the trust collapse),
`docs/architecture/SECURITY.md` rules 5 and 9

---

## Context

Compiling a Definition imports its `definition.py`, `policies/*.py`, `tools/*.py`, and
`mcp/*.py` **in-process** — top-level module code executes inside the orchestrator at
compile time. The framework documented this as acceptable because **a human wrote those
files**: `docs/reference/definition.md` states "Compiling imports `definition.py`,
`policies/*.py`, and `tools/*.py`: this is authoring-time trusted code."

`craw code` collapses that assumption. The author is now an LLM agent that may have read
fluid (untrusted) data — a poisoned ticket, a RAG hit — before it wrote those files. A
prompt-injected agent can therefore author a `tools/notify.py` whose *import-time* code
shells out, opens a socket, or reads outside the project. Importing it in-process is
arbitrary code execution in the orchestrator, steerable by the injection. The eval gate
catches quality regressions; it does **not** catch a malicious-but-passing import. The
security spine's runtime protections (the `Jail` for host-side node code at *run* time,
taint propagation, the fluid→static-sink gate) all sit *downstream* of a compile that has
already executed the attacker's code.

The framework already owns the right primitive: the `Jail` abstraction (ADR 0016) runs
host-side node code out-of-process, scoped to an allowed folder, with the network denied
by default, propagating taint across the process boundary. It was applied at run time; the
compile-time import of agent-authored code needs the same confinement one phase earlier.

## Decision

Agent-authored `definition.py` / `tools/*.py` / `policies/*.py` / `mcp/*.py` are **no
longer authoring-time-trusted**. Two deterministic controls gate them:

1. **Provenance-stamped (CRA-266).** Every component file carries a per-file provenance
   row keyed by `(component_path, content_sha)` recording `authored_by`
   (`"human"` | `"craw-code"` | a loop id), `source_tainted`, and the taint label set. A
   file authored while the loop held fluid context is stamped `source_tainted=True` and
   carries `FLUID_TAINT`. Taint is monotonic (SECURITY.md rule 9): a re-record never drops
   the label without an audited `declassify`. The content sha stays a pure content hash —
   `authored_by` is *adjacent to* identity, never mixed into the sha — so a directory and
   its installed copy still compile byte-identical.

2. **Jailed at compile (CRA-267).** When a component's provenance is `authored_by !=
   "human"` (or unknown), its import is routed through `select_jail()` (`load_definition_jailed`):
   the project dir is bound **read-only** and `STATIC` (`allow_paths` is static-only — a
   `Flow.FLUID` path raises `StaticOnlyError` before any spawn, so a fluid value can never
   widen the jail), and `allow_net=False`. Only the typed shape crosses back, never live
   Python objects; the type registry is rehydrated across the boundary so
   `parameters_compatible` holds identically to an in-process compile. The jail's
   `out_taint` is recorded onto the component's CRA-266 row. Any `Denial` (folder escape /
   undeclared egress) is emitted as a `JAIL_VIOLATION` and the compile **fails closed**
   with `DefinitionLoadError` — the authored code never executes in the orchestrator.

**Human-authored** components keep the fast in-process path — no perf regression for the
human loop; the jail is the cost only agent-authored code pays.

This **supersedes** the "compiling `definition.py` is authoring-time trusted" assumption in
`docs/reference/definition.md` for any non-human-authored component. The spine is now
*enforced* at the compile boundary, not merely *taught*.

## Consequences

- A prompt-injected agent cannot achieve code execution in the orchestrator by authoring a
  malicious import: the import is confined, audited, and on any escape the compile fails
  closed before the typed shape is produced.
- Downstream gates can key on authorship: CRA-271 redaction, CRA-272 in-loop assembly gate,
  and the M6 promotion gate all consult `component_tainted(...)` and refuse
  agent-authored-under-injection code, because the provenance row tells them what authored
  a file and whether it was tainted.
- A jail `Denial` at compile maps to the `craw.error.v1` envelope `code="jail_violation"`,
  `retryable=false`, exit `2` (compile/jail failure) — an injected agent cannot retry past
  it.
- `docs/reference/definition.md` must be updated to scope its "authoring-time trusted"
  language to *human*-authored components and point to this ADR for agent-authored code.
- The jailed path adds out-of-process spawn cost to agent-authored compiles; this is
  deliberate and bounded to that case. Tests inject `FakeJail` (`SandboxPolicy(kind="fake")`)
  so the path stays deterministic and offline.

## Rejected alternatives

- **Keep trusting agent-authored imports, rely on the eval gate.** Rejected: the eval gate
  stops quality regressions, not a malicious-but-passing artifact, and it runs *after* the
  import has already executed. The trust boundary, not the quality bar, is what `craw code`
  breaks.
- **Static-analyze the authored Python instead of jailing it.** Rejected: sound static
  detection of arbitrary import-time side effects (egress, file reads, `exec`) is
  undecidable in general and trivially evaded; the jail is a fail-closed capability boundary
  that does not depend on out-guessing the attacker.
- **A second, separate "safe compile" execution path for agent code.** Rejected: a second
  execution path is exactly what RFC 0001 warns against (divergence, drift, two things to
  audit). This wires the *existing* `Jail` seam into the *one* compile path, gated by
  provenance — humans and agents hit the same loader, the jail is the only added phase.
- **Trust the file if a human later signs it, with no compile-time confinement.** Rejected:
  signing (the SEC-1 admissibility gate) governs whether a *recorded* artifact may fire a
  consequential action; it does nothing about the arbitrary code that runs the instant the
  unsigned, unreviewed file is imported. Confinement must happen at import, before any
  human is in the loop.
