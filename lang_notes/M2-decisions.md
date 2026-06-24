# M2 — Composition surface (CRA-205/206/207/208): ARCH + SECURITY review

Reviewer: `review-m2` (combined architecture + security). Scope: the cyclic/runnable
composition surface in `packages/crawfish/src/crawfish/workflow.py` (Program, Edge, branch,
Recurse, recurse, the ROUTER arm of `_run_step`), the F-2 ledger coordinates in
`ledger.py`, and the three acceptance suites. Commit under review: `90cb464`.

## Verdicts

- **ARCH: PASS.** The composition surface reuses one kernel (`_run_step` / `check_types` /
  the F-2 ledger); Program is a `Workflow` subclass and Recurse a tagged `AGGREGATOR`, so no
  core `NodeKind` widens. Cyclic `check_types` routes through structural `parameters_compatible`,
  never string equality. The four fork decisions are sound.
- **SECURITY: PASS-WITH-NOTE.** Cycle and recursion bounds fail closed at assembly and stop
  cleanly at runtime (never wall-clock); FLUID never reaches a static slot (labels gate a
  closed static branch set, coordinates fold static shas + stable lineage); taint is unioned
  across every edge / level / fold; recurse re-enters only the frozen content-hashed body;
  resume is org-isolated. Two non-blocking notes below (N1 the recurse-as-Program-step
  integration gap, N2 a no-progress preflight asymmetry) — neither widens the trust boundary.

**BLOCK-level defects: none.**

## Fork decisions — confirmed

### D-M2-1 — Program is a Workflow, not a new NodeKind (CRA-206, arch). CONFIRMED.
`Program(Workflow)` (workflow.py:401) adds a driver, not a leaf kind; it dispatches existing
kinds through the inherited `_run_step`. This is the correct altitude: cyclic capability is a
composition concern, and the `NodeKind` enum (owned elsewhere) stays closed. Consistent with
M1 Refine. **Spine impact:** none (no type-system widening).

### D-M2-2 — Router is a runnable step through the SAME _run_step (CRA-205, arch+security). CONFIRMED.
The ROUTER arm (workflow.py:317-337) re-enters `_run_step(br, [item], ...)`, so a branch
(Sink/Batch/Filter/Aggregator) inherits identical budget/taint/checkpoint — no duplicated
dispatch that could lose a guarantee (audit Gap #3 closed). The classifier label is a
fluid-derived *control signal* that selects WHICH member of a closed, assembly-fixed branch
set fires; it never becomes a consequential target. A tainted item routed into a static-only
Sink still raises inside that Sink (spine invariant 2/5 preserved). The same pattern is reused
correctly inside `_run_region_pass` (workflow.py:678-686). **Spine impact:** fluid-boundary —
upheld.

### D-M2-3 — loop_id = compute_loop_id(region_version, item_lineage, edge_id) (CRA-206/207, both). CONFIRMED.
`region_version` (workflow.py:692-708) folds the content shas of the region's frozen Batch
Definitions (falling back to step names), so the coordinate is content-addressed: a body
change mints a fresh loop_id. `compute_loop_id` (ledger.py:25-37) is a length-prefixed
sha256 — derived, never `new_id()` — so a second process re-derives the identical coordinate
and resume re-charges $0 (verified `test_durable_resume_recharges_zero`). `_LOOP_ID_VERSION`
gates the migration if composition changes. The three inputs are all STATIC or
stable-lineage; none is fluid output content. **Spine impact:** versioning + fluid-boundary —
both upheld.

### D-M2-4 — recurse reuses AGGREGATOR + node_kind_tag; coord {content_sha}#{edge}#d{depth} (CRA-208, both). CONFIRMED.
`Recurse.kind = NodeKind.AGGREGATOR` + `node_kind_tag = "recurse"` (workflow.py:757,781) —
reduce-shaped, no enum widening, identical to Refine's tagging. The depth coordinate
`{body.content_sha()}#{edge_id}#d{depth}` (workflow.py:841) and `loop_id` keyed on
`body.content_sha()` (workflow.py:794) re-enter only the FROZEN content-hashed Definition;
each level `derive()`s a fresh frozen Output (no in-place mutation). **Spine impact:**
versioning — upheld.

## Security invariants — explicit confirmation

**Cycle / recursion bounds fail closed.**
- Assembly: a back-edge with `max_visits is None` raises `UnboundedCycleError`
  (workflow.py:483-488); `recurse(..., max_depth=None)` raises `UnboundedRecursionError`
  at construction (workflow.py:773-776). Both fail before any run.
- Runtime: the loop ceiling is `for visit in range(edge.max_visits)` (workflow.py:603) and
  `for depth in range(self.max_depth)` (workflow.py:826) — hard counted bounds. Additional
  guards are budget preflight (`remaining_usd <= 0`, workflow.py:607-610 / :830-833),
  cooperative cancel before each step (:604 / :827), and calibrated no-progress (:638-647 /
  :854-862). **No wall-clock anywhere.** Bound trips → `on_stuck` (`dead_letter` |
  `return_last`) → clean stop. Confirmed by `test_budget_hard_stops_*`,
  `test_unbounded_back_edge_raises_at_assembly`, `test_never_base_case_halts_at_max_depth`.

**FLUID never reaches a static slot.**
- Branch labels select among a closed branch set fixed at assembly (totality enforced by
  `UnroutableLabelError` at construction); a label cannot synthesize a new target.
- Ledger coordinates / `produced_by` are built from `region_version` / `body.content_sha()` /
  `edge_id` / `visit` / `depth` — all static or counted, never fluid output content.
- `loop_id`'s only per-item input is `item_lineage` (the stable lineage cursor, not the
  fluid value), so idempotency identity is not fluid-derived (spine invariant 3).
- Sink-target staticness is enforced inside the Sink (`TargetMustBeStaticError`,
  `test_fluid_sink_target_rejected_at_construction`); the Router arm does not bypass it.

**Taint propagates (union) across every boundary.**
- Cycle: `tainted=bool(current.tainted or prev.tainted)` per derive (workflow.py:625);
  Router branch carries lineage/taint forward (:336); `test_taint_carries_across_the_cycle`.
- Recurse: per-level union (workflow.py:843) and fold union
  `any(c.tainted for c in children) or seed.tainted` (workflow.py:869) — "a vote/fold does
  not launder taint" (`test_combine_does_not_launder_taint`).

**Recurse re-enters only a FROZEN Definition.** `compute_loop_id(self.body.content_sha(), …)`
and the depth `produced_by` both bind `body.content_sha()`; `_run_level` runs `Run(self.body,
…)` against the frozen body and threads the shared `ctx` so the one whole-tree budget meters
descent (the real `O(b^d)` guard). No un-versioned mutation is persisted.

**Durable resume is cross-org isolated.** Every `ledger_loop` row carries `org_id`
(ledger.py:140-151 / :174-187); `completed_visits` / `completed_depths` read via
`list_records(org_id=self._org)` (ledger.py:153-163 / :189-195). A resume under org-b cannot
observe org-a's committed visits — confirmed `test_ledger_rows_carry_org_id_cross_org_isolation`
and `test_resume_rows_carry_org_id` (both assert org-b sees `[]`).

## Notes (non-blocking)

### N1 — recurse does not yet inherit a Program's ledger when used as a step (security: low).
`Recurse` is a `Node` with its own `execute(seed, ctx, runtime, *, ledger=None, resume=False)`
signature (workflow.py:796-805), but `_run_step` has **no arm for a `Recurse` step** — it
would fall through to `NodeKind.AGGREGATOR`? No: `_run_step` dispatches on `isinstance`, and
`Recurse` is not an `Aggregator` subclass, so a `Recurse` placed as a Program/Workflow step
hits `raise TypeError(... step.kind)` at workflow.py:339. Today recurse is exercised only via
its standalone `.execute(...)` (as the tests do), so this is not a live defect — but the
changelog frames recurse as "a Program back-edge," which implies step-level composition that
the dispatch does not yet support. **Fix (when composition lands, not now):** add an
`isinstance(step, Recurse)` arm to `_run_step` that calls `step.execute(item, ctx, rt,
ledger=<the driver's ledger>, resume=<driver resume>)`, so a recurse-as-step inherits the
shared budget/ledger/resume rather than running ledger-less. Until then, document that
`recurse(...)` is a standalone executor, not yet a drop-in `.step(...)` node.

### N2 — no-progress preflight asymmetry on replay (determinism: cosmetic).
In both drivers the no-progress band is skipped while `replaying` (workflow.py:639 / :854),
which is correct (a replayed delta must not change control flow). But the budget preflight is
also skipped while replaying (:606 / :829) — correct — yet `visits`/`depth_reached` still
increment on the replayed pass. This is intentional (the cursor must advance), and resume is
content-hash-verified downstream, so there is no divergence; flagging only so a future reader
does not mistake the asymmetry for a bug. No change required.

## Bottom line
The composition surface holds the spine: one kernel, structural type-compat, bounds that fail
closed with no wall-clock, a closed static branch/coordinate space that fluid input can select
but never widen, union taint across every cycle/level/fold, frozen-only recursion re-entry, and
org-scoped durable resume. ARCH PASS; SECURITY PASS-WITH-NOTE (N1/N2, neither a trust-boundary
defect). No source edited; no git run.
