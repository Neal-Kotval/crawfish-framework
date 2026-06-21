# Build log — doc authoring

## Reference docs — COMPLETE (2026-06-21)

Built the explained, layered reference (`docs/reference/`) covering **every** symbol in
`crawfish.__all__`. Each page is three-tiered (Core → Ramps up → API reference) with a
deterministic runnable example, matching the gold-standard exemplar `core-types.md`.

- **31 pages** authored (1 exemplar + 30 cluster pages + index), **332/332** public
  symbols documented — coverage gate `scripts/check_docs_coverage.py` passes 100%.
- **30/30 examples** run clean-room, deterministic (run twice, identical), and match the
  shown `▶ Output` byte-for-byte — gate `scripts/verify_examples.py`.
- `mkdocs build --strict` clean (exit 0); `Reference` nav section wired; every ADR/cross
  link resolves; flat API dump `docs/guide/api-reference.md` regenerated.
- MkDocs setup: added `pymdownx.details` + `pymdownx.tabbed` extensions; synced the
  `docs` dependency group. Examples use hardcoded, sandbox-verified output blocks (no
  `markdown-exec` dependency).
- Process: parallel authoring subagents (one page each, source-verified), then a cold
  review pass (concision/consistency/beginner-jargon) over disjoint page groups + a
  read-only security/architecture review of the spine pages.

Four behavioural findings surfaced while documenting (below). All are documented as
*actual behaviour* on the relevant page; none were smoothed over. The `sandbox-and-jail`
`allow_net` overstatement was **corrected on the page** (2026-06-21); the other three are
code-side suggestions left for the owning teams (docs describe real behaviour today).

## Doc-surfaced findings

- `crawfish.metrics.compare(a, b)` builds its result by iterating `set(a) | set(b)`, so the
  returned `dict`'s **key order is PYTHONHASHSEED-dependent** — repeated runs of the same
  inputs yield the same deltas but in different key order (`{'coverage':…, 'accuracy':…}`
  vs `{'accuracy':…, 'coverage':…}`). Values are correct and stable; only iteration order
  varies. Callers that print/serialise the dict for snapshots must sort keys first (the
  metrics.md example does). Consider building the result over `sorted(names)` for
  reproducible ordering.
- `crawfish.validation.ValidationError` is named like a Python exception but is a frozen
  Pydantic `BaseModel` (a failure *record*), not an exception. The validators return it
  in lists (`list[ValidationError]`); nothing `raise`s it. Documented the actual behaviour
  on `docs/reference/validation.md`. Consider renaming to e.g. `ValidationIssue` to avoid
  the implication that it is caught.
- `crawfish.cost.spent_today(store)` returns `0.0` unconditionally when `run_ids` is left
  at its `None` default — it short-circuits (`if run_ids is None: return 0.0`) *before*
  reading any events, so a meter wired with the bare `spent_today(store)` reports $0
  regardless of actual recorded spend. Verified by reproduction against a `SqliteStore`
  holding a `run.finish` event of $2.50: `spent_today(store)` → `0.0`,
  `spent_today(store, run_ids=["run-1"])` → `2.5`. Documented the actual behaviour on
  `docs/reference/cost-routing-cache.md`. The Store seam is per-run (no cheap cross-run
  scan), so the contract requires callers to pass `run_ids`; consider raising or warning
  on `run_ids is None` rather than silently returning $0.

### 2026-06-21 — Security/architecture review of security-spine reference pages

Verified four reference pages against source. The four named security claims all hold
true per source; one overstatement and a few worth-mentioning invariants below.

- **`nodes-router-sink.md` (vs `nodes/sink.py`, `router.py`) — ACCURATE.** Static-only
  sink target / `TargetMustBeStaticError` (raised in `Sink.__init__` for any non-STATIC
  `target_params` flow), `ApprovalRequired` (raised by `write` when `always_ask` and no
  `approve` callback), and the ordering claim "approval gate fires *before* the
  idempotency claim" all match source exactly. `_idempotency_key` hashes sink name +
  `ctx.batch_id` + `output.lineage or output.id` + sorted static `config` and excludes
  the `Output` value, as documented. No issues.

- **`secrets-and-consent.md` (vs `secrets.py`) — ACCURATE.** `redact` replaces known
  values and the built-in patterns with `***REDACTED***`; secrets held by reference
  (`resolve_secret`/`SecretManager.for_node` least-privilege); `consent_install` defaults
  to `DenyConsent` (fail-closed) and writes no grant + raises `ConsentDeclined` on decline.
  `ScrubbingStore` redacts only write paths (`put_record`/`kv_set`/`append_event`); read/
  admin paths incl. `claim_idempotency` are pass-through, exactly as the doc's table says.
  No issues.

- **`secret-broker.md` (vs `secrets.py`) — ACCURATE.** `LeaseHandle` carries `lease_id`/
  `ref`/`destination`/`node_id` but never the value, and its `__repr__` omits `lease_id`.
  The five-check `lease` gate (static-only → granted secret → granted destination → value
  present → approved) raises `LeaseDenied` on every failure. `send` re-materialises by
  `lease_id`, refuses `host != leased destination`, injects the credential as a `Bearer`
  header at egress, and returns only the transport response — the value never crosses to
  the child. `QueuedApprovalQueue` default is `False` (fail-closed) and keys decisions by
  `(node_id, ref, destination)` so a single approval survives retries. No issues.

- **`sandbox-and-jail.md` (vs `jail.py`, `sandbox.py`) — MOSTLY ACCURATE, one
  overstatement.** Out-of-process execution (`run_out_of_process` via `ProcessPoolExecutor`)
  ✓; `StaticOnlyError` raised by the shared `Jail._check_static`, which every backend
  (`FakeJail`/`NoJail`/`_RealJail`) calls first thing in `run`, before any spawn ✓; taint
  propagation — `FakeJail` adds `FLUID_TAINT` when the child emits fluid output, connects
  to the network, OR produces any denial; real backends carry input taint forward and
  presumptively taint when `allow_net=True` ✓; `emit_denials` writes one `JAIL_VIOLATION`
  per denial, `tainted=True` ✓.
  - **OVERSTATEMENT (low severity) — RESOLVED 2026-06-21 on `sandbox-and-jail.md`:** the
    static-only guarantee covers `allow_paths`, NOT `allow_net`. (Page now states
    `allow_paths` is static-only enforced via `_check_static`, and `allow_net` is a static
    `bool` with no fluid representation.) The page repeatedly couples the two — Core: "which folders **and whether
    the network is open** are static-only"; the "Static-only is enforced…" section:
    "`allow_paths` **and** `allow_net` derive from static node config only"; and the
    `StaticOnlyError` API entry: "offered where only static is permitted
    (`allow_paths`/`allow_net`)." In source, `_check_static` inspects only `allow_paths`
    (each `JailPath` carries a `flow` field). `allow_net` is a bare `bool` parameter with
    no provenance/`flow` field, so there is no mechanism that can reject a "fluid"
    `allow_net` — a `Flow.FLUID` `allow_net` is structurally unrepresentable and therefore
    never raises `StaticOnlyError`. The guarantee is real for paths; for net it's a design
    convention (the param has no fluid form), not an enforced check. The docs should say
    "`allow_paths` is static-only (enforced); `allow_net` is a static `bool` with no fluid
    representation" rather than implying both are checked the same way.

- **Undocumented invariants worth surfacing (non-blocking):**
  - **`NoJail` does NOT propagate input taint as taint-amplification — it only carries
    input `taint` forward** (`out_taint=frozenset(taint)`); it never *adds* `FLUID_TAINT`
    even if its out-of-process child touched the network or fluid data, because it does no
    probing. The page correctly says `NoJail` "propagates taint" but a reader could infer
    it taints network-touching output the way `FakeJail`/real-net backends do. Worth a
    one-line caveat that `NoJail` is taint-passthrough only, reinforcing "never the default
    for fluid code."
  - **The real `BwrapJail`/`SeatbeltJail` backends do not surface folder/egress `Denial`s
    on `JailResult.denied`** — only a `TIMEOUT` denial is ever attached (the kernel/Seatbelt
    blocks escapes pre-emptively, so a blocked escape shows up as a nonzero `exit_code`, not
    a `Denial`). Only `FakeJail` enumerates `FOLDER_ESCAPE`/`UNDECLARED_EGRESS` denials. The
    page's "Denials are blocked *and* audited" section implies real backends feed
    `emit_denials`; in practice, on the real backends `emit_denials` writes nothing for a
    blocked-but-not-timed-out escape. Worth noting the audit trail of *individual* escape
    attempts is a `FakeJail`/conformance-suite property, not a real-backend runtime one.
