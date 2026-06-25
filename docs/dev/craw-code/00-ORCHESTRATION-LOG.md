# craw code — Build Orchestration Log

This document records the **key architectural decisions and the orchestration process**
for building `craw code` end-to-end, as required by the build prompt. It is the dev-facing
companion to the user-facing specs in [`docs/specs/craw-code/`](../../specs/craw-code/00-README.md)
and RFC [`0001-craw-code`](../../rfcs/0001-craw-code.md).

## Orchestration model (decisions of the orchestrator)

The build prompt asks for one-owner-per-file parallelism across 9 milestones via git
worktrees, with PR gates reviewed by standing security/architecture/qa specialists, merged
to an integration branch, then a final PR to `main`. Concrete decisions taken to execute
that safely:

1. **A new `crawfish/code/` subpackage houses the entire `craw code` verb family.** This
   is the single most important collision-avoidance decision. Every milestone owns
   *disjoint new modules* inside `crawfish/code/`, plus *disjoint test files* (the spec's
   per-issue `test_code_*.py` names give the exact partition). No two milestone agents edit
   the same file.

2. **Subcommand registration is auto-discovered, not hand-wired.** `crawfish/code/__init__.py`
   exposes a registry that discovers `register(subparsers)` hooks in sibling modules via
   `pkgutil`. Adding a new verb is adding a new file — never editing a shared dispatcher.
   The only one-time edit to the top-level `crawfish/cli.py` (wiring the `code` group) is
   done **once** in Wave 1 and never touched again.

3. **Wave 1 is a single cohesive "foundation" agent, not parallel.** The contracts
   (`craw.error.v1` envelope CRA-270, `--json` schema negotiation CRA-269, exit-code audit
   CRA-243, provenance record format CRA-266) and the M0 keystone (provenance CRA-266,
   jailed compile CRA-267, loop harness CRA-268) all live on the *same* shared foundation
   files (`crawfish/code/__init__.py`, `provenance.py`, `jail.py`, the registry). Splitting
   them across agents would violate one-owner-per-file. They are one interlocking contract,
   so one deep agent builds them. Everything downstream keys on this, so Wave 1 fully lands
   and is gated **before** any Wave 2 worktree branches from integration.

4. **Worktree isolation + orchestrator-side integration.** Each milestone agent works in
   its own `git worktree` off `craw-code/integration`, runs its local DoD green, and reports
   its branch. The orchestrator merges each branch into integration, re-runs the *full* suite
   at integration level to catch cross-milestone breakage, and only then runs the specialist
   review gate. This keeps parallel builds safe while keeping integration correctness at the
   orchestrator (the one actor that sees all milestones at once).

5. **Standing specialists are review *passes*, not long-lived processes.** `security-agent`,
   `architecture-agent`, and `qa-agent` are re-spawned against each integrated diff with the
   authority to BLOCK. A block returns a fix-list to the milestone agent; the loop repeats
   until all three sign off.

## File-ownership partition (one owner per file)

| Milestone | Owns (source, under `crawfish/code/` unless noted) | Owns (tests) |
| --- | --- | --- |
| **Wave 1 foundation (M0+contracts)** | `code/__init__.py` (registry, `craw.error.v1`, schema negotiation, exit codes, provenance record), `code/cli.py` (`craw code` group), `code/harness.py`; extends `provenance.py`, `jail.py`; one-time edit to top-level `cli.py` | `test_file_provenance.py`, `test_jailed_compile.py`, `test_authoring_harness.py`, `test_error_envelope.py`, `test_schema_negotiation.py`, `test_cli_json_coverage.py` |
| **M1** foundations & CLI | `code/describe.py`, `code/estimate.py`; extends `build.py` assembly-gate-in-run | `test_describe.py`, `test_describe_redaction.py`, `test_describe_cache.py`, `test_code_estimate.py`, `test_run_assembly_gate.py`, `test_code_org_isolation.py` |
| **M2** scaffolding | `code/init.py`, `code/new.py`, `code/sync.py`, `code/map.py`, `code/adopt.py`, `code/templates.py`, `code/lint.py` | `test_code_init.py`, `test_code_new.py`, `test_code_sync.py`, `test_code_map.py`, `test_code_adopt.py`, `test_code_explain.py`, `test_code_init_reentrant.py`, `test_code_tree_lock.py`, `test_code_consent_regate.py`, `test_code_lint.py` |
| **M3** plugin + skills | `plugin/` bundle (`.claude` plugin, `plugin.json`, skills, commands) | `test_plugin_skills.py`, `test_plugin_commands.py`, `test_plugin_pin.py` |
| **M3a** authoring | `plugin/skills/authoring/*` (per-file authoring skills), golden example under `demo/` | `test_authoring_spec.py`, `test_golden_definition.py`, `test_authoring_validation.py` |
| **M4** dashboard | `code/dashboard/` (`data.py`, views, server, encoding) | `test_code_dashboard_seam.py`, `test_code_dashboard_data.py`, `test_code_dashboard_runs.py`, `test_code_dashboard_xss.py`, `test_code_dashboard_cost.py`, `test_code_dashboard_optimize.py` |
| **M4.5** operate | `code/optimize.py`, `code/deploy.py`, `code/control.py` | `test_code_optimize.py`, `test_code_deploy_fleet.py`, `test_code_control.py` |
| **M6** HITL | `code/gate.py`, `code/review.py`, `code/diagnose.py`, PreToolUse hook | `test_code_gate.py`, `test_code_review.py`, `test_code_diagnose.py` |
| **M5** MCP veneer | `code/mcp.py` (thin, 4 meta-tools over the CLI) | `test_code_mcp.py` |

Coordination points reconciled by the orchestrator at integration time only:
`crawfish/__init__.py` exports, `mkdocs.yml` nav, `demo/` shared assets.

## Dependency-ordered waves

- **Wave 1**: foundation (above) → lands + gated on integration.
- **Wave 2** (parallel): M1, M2-core (init→new→sync).
- **Wave 3** (parallel): M2-rest (map/adopt/templates/consent/treelock), M3, M3a-spec+golden.
- **Wave 4** (parallel): M3a per-file skills + eval, M4 dashboard.
- **Wave 5** (parallel): M4.5 operate, M6 HITL.
- **Wave 6** (serial): M5 veneer, full-system QA + live demo.

## ADRs written for this build

- `decisions/0010-jailed-compile-agent-authored-code.md`
- `decisions/0011-observersurface-dashboard-seam.md`
- `decisions/0012-export-relationship-adopt-subsumes-export.md`

(Numbers chosen sequentially after the highest existing ADR; the README's tentative 0008/0009
were placeholders — resolved here.)

## Status ledger

Updated as waves complete. See git log on `craw-code/integration` for the authoritative trail.

| Wave | Milestone | Branch | Built | Integration suite | Gate (sec/arch/qa) |
| --- | --- | --- | --- | --- | --- |
| 4 | M3a per-file authoring skills + validation eval verb (CRA-258..265, UNFILED-OPT) | `craw-code/m3a` → merged | ✓ | ✓ 1592 passed | gated ✓ (all PASS) |
| 4 | M4 dashboard — seam/XSS/cost + ADR 0011 (CRA-252/253/254, UNFILED-SEAM/XSS/COST) | `craw-code/m4` → merged | ✓ | ✓ 1592 passed | gated ✓ (all PASS; live XSS render proof) |
| 5 | M4.5 operate — optimize/deploy/fleet/cancel/resume (UNFILED-OPTIMIZE/DEPLOY/CONTROL) | `craw-code/m45` → merged | ✓ | ✓ 1637 passed | gating |
| 5 | M6 HITL — gate (propose/apply/reject) / review / diagnose (UNFILED-GATE/REVIEW/DIAGNOSE) | `craw-code/m6` → merged | ✓ | ✓ 1637 passed | gating |
| docs | 9 guide+reference pages + mkdocs nav | `craw-code/docs` → merged | ✓ | ✓ (additive) | — |

**Wave 5 notes:** Session was interrupted (agents stopped mid-work); re-dispatched m45/m6 to complete from their uncommitted state. Caught & fixed: (a) two real bugs in optimize.py (empty knob grid, missing max_iters) — found by m45; (b) exit codes 4/5/6 escaping the closed table → normalized (over-budget→3, no-baseline→2, raced-done→1, granular in detail.exit); (c) a **flaky M4 test** — `dashboard/optimize.py` lineage relied on `list_records`' coarse `updated_at` order with no tie-breaker → ~15% flake; fixed to order by parent-chain depth (0/30 flakes after). m6 built a durable Store-backed `ApprovalLedger` (vs in-memory `QueuedApprovalQueue`) because propose→approve→apply spans processes; fail-closed, identity-keyed (component,sha), org-scoped, with a PreToolUse hook + 3 red-team payloads. 23 verbs now registered.
| 1 | foundation (CRA-266/267/268/269/270/243, ADR 0010) | `craw-code/foundation` → merged `e2a37c5` | ✓ | ✓ 1325 passed | gated ✓ (all PASS) |
| 2 | M1 describe/estimate/contracts (CRA-244/271/272/273/274/275) | `craw-code/m1` → merged `1e101f1` (+seam fix) | ✓ | ✓ 1382 passed | gated ✓ (all PASS) |
| 2 | M2-core init/new/sync (CRA-245/246/247) | `craw-code/m2` → merged | ✓ | ✓ 1382 passed | gated ✓ (all PASS) |
| 3 | M2-rest (CRA-276/277/278/279, UNFILED-MAP/ADOPT, ADR 0012) | `craw-code/m2` → merged `ddbf146` | ✓ | ✓ 1526 passed | arch+qa PASS; **sec BLOCK** → fix `craw-code/m2fix` |
| 3 | M3 plugin + skills + commands + pin (CRA-248/249/250/251, UNFILED-PIN) | `craw-code/m3` → merged `eecca73` | ✓ | ✓ 1526 passed | arch+qa PASS |
| 3 | M3a authoring spec + golden (CRA-256/257) | `craw-code/m3a` → merged | ✓ | ✓ 1526 passed | arch+qa PASS |

**Wave 3 SEALED** (gate: arch+qa PASS, sec PASS after fix). Final guard: `grep` confirms **no bare `load_definition(` remains in any `code/` verb** — every authored-code compile path goes through `load_definition_jailed`. 1529 tests green.

**Residual hardening note (follow-up, pre-existing — not a Wave-3 regression):** the shipped jail backend is `SandboxPolicy(kind="fake")` (FakeJail), which *certifies-then-imports in-process* — used uniformly by describe/estimate/harness/adopt/map/consent/sync. True out-of-process OS isolation requires swapping a real jail backend via the `select_jail`/`SandboxPolicy.kind` seam. Candidate for a dedicated ADR before any `--live` host-execution path ships.

**Wave 3 fixes (resolved):** Two latent defects caught at integration — (a) `code/new.py` policy template emitted invalid `Policy(description=)` (caught by m3a) → fixed; (b) tree_busy/not_a_project returned literal process exit 8/9 escaping the closed 0-4 table → normalized to exit 1/2 with granular codes in `detail.exit`. **Security gate BLOCK (load-bearing):** `adopt`/`map`/`consent grant` called the UNJAILED `load_definition`, executing untrusted authored code in-process — the exact trust-collapse hole. Fix agent `m2fix` routes them through `load_definition_jailed` + adds a red-team exfil test; also fixes consent_required `detail.exit` 3→4 collision. Re-gated by sec-w3 before sealing Wave 3.

**Deferred follow-ups (qa-w3 non-blocking, no spine impact):** `new mcp --dir <project-root>` writes an inert top-level MCP that escapes the consent scan (require a Definition target); missing-assertion coverage gaps — CRA-256 skill↔source no-drift test (fold into M3a Wave 4), CRA-278 `craw doctor` torn-tree check, UNFILED-MAP edge-structure assertion, CRA-276 JWT class, CRA-279 `--upgrade` re-pin field.

**Integration fixes (orchestrator-reconciled):** m1's `describe.py`/`estimate.py` instantiated `SqliteStore()` directly — caught only at integration by m2's `test_init_no_concrete_store_import_in_code_pkg` grep guard (cross-milestone breakage neither isolated suite could see). Delegated to m1; fixed to use `manage.store_for_dir` factory (`a4172e3`). Non-blocking nits noted for final docs pass: describe.py:288 docstring "exit 4"→"exit 3".
