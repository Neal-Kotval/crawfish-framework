# Claude Code prompt — Implement Milestone F with an agent team (code + docs in lockstep)

> Paste everything below the line into Claude Code at the repo root of `crawfish`.
> It tells Claude Code to orchestrate a team of subagents that implement Milestone F
> issue-by-issue (one owner per file, in dependency waves) while a docs steward keeps
> the documentation updated from each change, and a verifier enforces the Definition of Done.

---

## Mission

Implement **Milestone F — Foundations** of *The Agent Language* (Linear project, team `CRA`, issues **CRA-193 → CRA-201**). These are the one-owner primitives every later milestone depends on. Because each issue owns a distinct file/module, the work parallelizes cleanly across a **team of subagents**, gated by a dependency order. As code lands, the documentation must be updated **in the same pass**, not afterward.

You are the **orchestrator**. Plan the waves, spawn subagents, enforce the gate between waves, and do not let two agents edit the same file at once.

## Ground truth to read first (do this before spawning anyone)

1. `CLAUDE.md` — commands, layout, architecture rules, the security spine, working conventions.
2. `docs/roadmap/AGENT-LANGUAGE-EPIC.md` (§F-0 … §F-8) — the per-issue build spec each issue cites.
3. `docs/architecture/ARCHITECTURE.md`, `docs/architecture/SECURITY.md`, and `docs/architecture/decisions/` (existing ADRs).
4. `ROADMAP.md` and `docs/roadmap/README.md` — what shipped, what's next.
5. The current code each issue touches: `output.py`, `runtime/replay.py`, `ledger.py`, `eval.py`, `metrics.py`, `cost.py`, `sink.py` (`claim_idempotency`), `core/`, `versioning/`, `store/`.

If a Linear MCP is connected, read each issue (CRA-193…201) directly; otherwise rely on the epic spec sections above. (The plain-language companion lives in the Notion page "Agent Language Milestone F".)

## Non-negotiable rules (apply to every subagent)

**Definition of Done for any change:** `ruff` clean, `mypy --strict` clean, `pytest` green and **deterministic (no live model calls — use fixtures / record-replay cassettes)**, the security spine upheld, the demo (`demo/triage-bot/`) still runs end-to-end, and docs updated.

**Two-tier testing — both are required:**
1. **Deterministic unit/integration suite** (`pytest`) — never calls a live model; uses fixtures/cassettes; this is what CI runs.
2. **Live end-to-end acceptance run** — a real run of the extended demo against a real `AgentRuntime` backend (real model calls), exercising **all nine F features at once**, proving it actually works, then proving the recorded cassettes replay **bit-identically**. This is NOT in CI (it costs money and needs credentials) but it IS a hard milestone gate — the milestone is not done until the live run is green and its evidence is captured. See "Live end-to-end demo" below.

Commands (always via `uv` from repo root):
```bash
uv sync
uv run ruff check . && uv run ruff format --check .
uv run mypy packages/crawfish/src
uv run pytest -q
uv run craw run
```

Architecture & security constraints:
- **Three swappable seams** (`AgentRuntime`/`Store`/`ArtifactStore`): the product model imports their *protocols*, never a concrete backend. No SDK import in nodes; **no raw SQL outside a `Store` impl**.
- **Type compatibility is structural** via `crawfish.typesystem` — never string equality.
- **Pydantic for data shapes, ABCs for behavioural nodes**; enums are `(str, Enum)`.
- **Tenancy:** every `Store` row carries `org_id` (default `"local"`); prove cross-tenant isolation.
- **Security spine:** `Flow.FLUID` inputs are untrusted — they reach the model as data, never as instructions. Consequential Sink targets and idempotency keys are static-only; secrets resolved by reference, never logged or in-prompt.
- **One owner per file** — this is what makes the team safe. A subagent edits only the files it owns. If it needs a change in someone else's file, it records a request, it does not edit.

## Team roster (subagents)

Spawn these as subagents. Give each only its owned files and its acceptance criteria.

- **`impl-F0` … `impl-F8`** — one implementer per issue. Each: implement only its owned module(s), write the tests that encode its acceptance criteria, run the DoD checks for its scope, and write a short changelog snippet to `docs/_changelog/F-<n>.md` (create the folder) describing: what changed, public API added, migration/hash-bump notes, and which docs/ADRs need updating.
- **`docs-steward`** — owns the cross-cutting docs so implementers never collide there: `ROADMAP.md`, `docs/roadmap/README.md`, `docs/architecture/ARCHITECTURE.md`, `docs/architecture/SECURITY.md`, and writes the ADRs. It consumes the `docs/_changelog/F-*.md` snippets after each wave and folds them in. Implementers may edit their *own* spec section in `docs/roadmap/AGENT-LANGUAGE-EPIC.md`; the steward owns everything else.
- **`demo-runner`** — owns `demo/triage-bot/`. After Wave C, extends the demo with a single scenario that uses **all nine** F features (see "Live end-to-end demo"), provides both a cassette-backed deterministic test of it and a `--live` mode that hits a real backend. Owns only the demo dir, so it can't collide with implementers.
- **`verifier`** — a fresh agent (no implementation context) that, at the end of each wave and at the end, runs the full DoD, reads the diffs, checks the security spine (especially `org_id` on new rows and FLUID→sink boundaries), confirms tests are deterministic, **executes the live end-to-end run itself and pastes the evidence**, and signs off or sends specific fixes back. Use this agent for the security review and the live acceptance — do not let an implementer self-certify.

> Coordination protocol: before a wave, post the file-ownership map for that wave. After a wave, the orchestrator runs the full DoD, then `docs-steward` folds snippets, then `verifier` signs off. Only then start the next wave. Never run two agents that own the same file in the same wave.

## Dependency-ordered waves

**Wave A — decisions + leaf primitives (parallel; distinct files):**
- **CRA-193 · F-0** — `output_content_sha(o) = sha256(o.model_dump(mode="json"))` helper; reuse `Output.derive()` for the ledger key. Pure, no mutation. *Owns:* output/versioning helper. *Accept:* stable across processes; structurally-equal Outputs hash equal; `derive()` keeps propagated taint + a distinct `id`.
- **CRA-194 · F-1** — extend `_key` in `runtime/replay.py` to fold an optional content-hashed execution coordinate `{sample_index?, iter_index?, visit_count?, depth?}` + `org_id` + any decode field not in the content hash; legacy unsalted cassettes still resolve. *Owns:* `runtime/replay.py`. *Accept:* k recorded quorum samples → k distinct cassettes; same `(version, inputs, coordinate)` replays identically; legacy cassettes still hit; cross-tenant keys don't collide.
- **CRA-198 · F-5** — ADR: tunable decode knobs (`temperature`, `top_p`, `sample_k`) live in `AgentSpec`/Definition (enter the content hash); `RunRequest`'s value is **derived**, never set independently; `grammar` stays a per-call `RunRequest` property; declare an `AgentRuntime` determinism capability tier. Add the fields behind a content-hash version bump + documented re-freeze. *Owns:* the ADR + `definition`/`request` field additions. *Accept:* temperature has exactly one authoritative location, the other derived; re-freeze documented.
- **CRA-201 · F-8** — the experiment-design spec (primary-vs-guardrail metrics; pre-registered sizes OR anytime-valid sequential bounds; paired tests + family-wise correction; held-out tune-set vs gate-set split; winner's-curse correction). *Owns:* the spec doc + adoption hooks. *Accept:* it's a cross-cutting gate — later statistical consumers must cite it.
- **CRA-200 · F-7** — dynamic exclusive borrow with an atomic acquire via `with defn.mutable() as m:`; the registry is a **Store-backed atomic claim** reusing `claim_idempotency`'s tenancy-scoped pattern (`sink.py`), not an in-process dict. *Owns:* a new semantics/concurrency module + the Store claim. *Accept:* a concurrent acquire across async tasks raises `ExclusiveBorrowError` deterministically; sequential acquire/release round-trips.

**Wave B — depend on Wave A:**
- **CRA-195 · F-2** — new ledger key space `(loop_id, item_id, edge_id, visit) -> output_ref` (+ a depth variant for `recurse`), with migration; `loop_id = sha256(body.version.sha + item.lineage + edge_id)` — **never `new_id()`**; every row carries `org_id`. Uses F-0's sha for output refs. *Owns:* `ledger.py` / store ledger schema. *Accept:* two process runs of the same loop over the same item produce the same `loop_id`; resume re-charges \$0 for completed iterations; cross-`org_id` isolation holds.
- **CRA-196 · F-3** — single owner of the `eval.py` gate (must cite **F-8**): paired test over per-case deltas, family-wise (Holm) correction, Brier/NLL primary, `k` from a stated α, absolute-precision gate **fails closed** (no baseline ⇒ reject). *Owns:* `eval.py` / `metrics.py` gate. *Accept:* `std=0, k=0` reproduces today's `is_regression` byte-for-byte; a candidate within the paired noise band is rejected; precision gate raises with no baseline.

**Wave C — depend on Wave B (shares the eval domain → run after F-3):**
- **CRA-197 · F-4** — `GoldenSet.from_corrections` sourcing `human_revert`/`ci_failure`/`review_reject` from the Store ledger, plus a first-class `correction` emission kind; control who may emit a `correction` (provenance) and taint-analyze the corrected→guard path. *Owns:* eval corpus + the `correction` store event. *Accept:* count matches the ledger; cross-org isolation; `correction` kind is queryable; until it lands, dependents accept an authored `GoldenSet`.

**F-6 (CRA-199) — decision required, do not implement blindly.** This issue has no acceptance criteria of its own; it names **OPT-2 (CRA-220)** as the canonical cost spec. **Default action:** treat it as governance only — do *not* write cost code here. `docs-steward` records the "one owner of `cost.py`; CL-3/ALG-5 are consumers" note where OPT-2 will live, and the orchestrator flags F-6 for the human to confirm cancel-vs-keep. Ask before doing more.

## Docs updated in lockstep (the "simultaneous" requirement)

For **every** issue that lands, the owning implementer writes `docs/_changelog/F-<n>.md`, and `docs-steward` then propagates into:
- `ROADMAP.md` and `docs/roadmap/README.md` — move the item to shipped; note follow-ons.
- `docs/architecture/ARCHITECTURE.md` — any seam/contract change (e.g. the `_key` schema, the ledger key space, the determinism capability tier).
- `docs/architecture/SECURITY.md` — tenancy/`org_id` additions, FLUID→sink boundaries, the `correction` provenance control.
- `docs/architecture/decisions/` — new ADRs: **F-5 decode-knob ownership**, **F-7 borrow-lifetime semantics**, and a content-hash-version-bump/migration ADR (covers F-1, F-2, F-5 re-freeze).
- `docs/roadmap/AGENT-LANGUAGE-EPIC.md` — implementers tick their own §F-n acceptance as met.
- If a Linear MCP is connected: set each issue's priority (Urgent: F-0, F-1; High: F-8, F-3, F-2, F-5; Medium: F-4, F-7), add the F-3→after→F-8 blocker, and mark each `Done` only after the verifier signs off.

A wave is not complete until its docs are merged. Do not batch all docs to the end.

## Live end-to-end demo (required — prove it actually works)

After Wave C, `demo-runner` extends `demo/triage-bot/` with **one scenario that uses all nine F features together** — the "nightly self-improvement + safe production run" flow:

1. Open a `RunContext(org_id=…, budget=…)` — **F-1/F-2** tenancy + budget.
2. `with defn.mutable() as draft:` — **F-7** exclusive borrow (train mode).
3. Expose `temperature` as a tunable knob — **F-5**.
4. Build the eval set via `GoldenSet.from_corrections(...)` — **F-4**.
5. Split into tune-set / gate-set — **F-8**.
6. `estimate_cost(...)` and assert worst-case ≤ budget — **F-6** consumer (uses existing `cost.py`).
7. Tune the knob on the tune-set, then run the promotion **gate** on the gate-set — **F-3** (+ **F-8** winner's-curse shrink).
8. `freeze()` the winner — new `Version.sha`.
9. In eval mode, run a refine-style loop over a ticket with per-iteration `coord` — **F-1**; checkpoint each visit — **F-2**; stop when `output_content_sha` is unchanged — **F-0**.
10. Fire the Sink (`send`) — allowed only because the definition is frozen.

Provide **two ways to run it**:

- **Deterministic** (`pytest`, CI): runs steps 1–10 entirely off recorded cassettes; asserts the gate decision, the \$0-resume, and the no-progress stop. No live calls.
- **Live** (`uv run craw demo --live` or an equivalent script flag, gated by real credentials / a `crawfish.toml` profile with a real `AgentRuntime`): actually calls the model, records fresh cassettes, and must demonstrate, with captured output:
  - the flow completes and produces a real reply;
  - the promotion gate fires correctly (promote **or** a justified reject) under real variance;
  - the budget is respected (worst-case estimate ≥ actual spend);
  - **crash-resume works live**: kill the eval loop mid-way, re-run, and show the ledger re-charges **\$0** for completed visits and finishes;
  - cross-tenant isolation: a second `org_id` does not read the first's cassettes/ledger rows;
  - **replay determinism**: re-running the just-recorded scenario from cassettes is **bit-identical** (diff the two outputs by `output_content_sha`; they must match).

`demo-runner` writes a short `demo/triage-bot/RUNBOOK.md`: how to set credentials, the exact live command, expected output, and the captured evidence from a real run. Document cost (keep the budget small).

> Live calls cost money and need credentials. If credentials are not available in this environment, the agent must **stop and ask the human to run the live command**, providing the exact command and the checklist of evidence to capture — it must not fake a live run or mark the milestone done without real evidence.

## Verification & sign-off

After each wave: orchestrator runs the full DoD; `verifier` (fresh agent) reads the diffs and checks — deterministic tests (no live calls), `org_id` on every new row, FLUID never reaching instructions or a static-only sink, the byte-for-byte back-compat assertions (F-1 legacy cassettes, F-3 `std=0,k=0`), and that the docs match the code. The verifier returns either "signed off" or a specific fix list. Address fixes before the next wave.

**Final gate (after Wave C + demo):** the `verifier` must run the **live end-to-end demo itself** (or, if credentials are unavailable, hand the human the exact command and evidence checklist and wait for the results). The milestone is only "done" when: full `pytest` is green and deterministic; the live run completed with a real reply; the gate, budget, live crash-resume (\$0), cross-tenant isolation, and bit-identical replay evidence are all captured in `demo/triage-bot/RUNBOOK.md`; all docs/ADRs are merged. No faked or skipped live evidence.

## Kickoff

1. Read the ground-truth files. Print the file-ownership map and the wave plan, and call out any file two issues would touch (resolve by sequencing within a wave).
2. Confirm the **F-6 decision** with me before treating it as anything other than governance/docs.
3. Run Wave A. Gate. Run Wave B. Gate. Run Wave C. Gate.
4. `demo-runner` builds the all-nine-features demo (deterministic + `--live`) and the RUNBOOK.
5. `verifier` runs the **live end-to-end acceptance** and captures the evidence (or hands the human the exact command if credentials are missing). Do not declare done without real live evidence.
6. Finish with a full-suite green run, the live evidence captured, all docs merged, ADRs written, and a short report: per-issue status, new public APIs, migrations/hash-bumps, the live-run results, and anything deferred.

Work in dependency order, keep one owner per file, and keep the docs moving with the code.
