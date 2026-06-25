# craw code — Implementation Spec: Scaffolding, Plugin & Authoring (M2, M3, M3a)

This spec covers the second half of `craw code` (RFC [0001-craw-code](../../rfcs/0001-craw-code.md)):
the **scaffolding** verbs that stand a project up (`init`/`new`/`sync`/`map`/`adopt`), the
**Claude Code plugin** (skills + slash-command wrappers that teach the security spine,
the pipeline mental model, and determinism), and the **authoring playbook** — the
file-by-file definition-contents skills plus the golden worked example and the validation
eval. The companion spec `01-foundations-and-cli.md` (M0/M1: provenance, jailed
compile, `--json`/`craw.error.v1` contracts, `craw code describe`, `estimate`) is a
dependency for nearly everything here; where a verb consumes an M0/M1 seam it is named.
Two hard constraints frame the work: (1) `craw code` **drives the one execution path that
already ships** — every verb composes existing `craw` machinery (`docs/guide/cli.md`) and
imports protocols, never backends (`CLAUDE.md`); and (2) the **security spine is
enforced, not merely taught** — agent-authored capabilities re-enter the consent gate
(`crawfish.provenance.regate_generated`, `crawfish.secrets.consent_install`) and fluid
inputs never reach a static-only sink target (`docs/architecture/SECURITY.md`).

---

## Plugin package layout

The plugin is a directory shipped inside the `crawfish` distribution at
`packages/crawfish/src/crawfish/plugin/` and installed (copied / symlinked) into a
project's `.claude/` tree by `craw code init`/`adopt`. It uses **only stable Claude Code
plugin component conventions**: a `.claude-plugin/plugin.json` manifest, auto-discovered
`skills/<name>/SKILL.md`, an optional `commands/` dir of thin slash-command wrappers, and
(deferred to M5) an optional `.mcp.json`. Per the CC platform facts, **plugin-shipped
subagents cannot carry `hooks`/`mcpServers`/`permissionMode`** — so this plugin ships
**knowledge (skills) and command veneers only**, never an agent that re-grants itself
capabilities. Every component name is namespaced under a reserved **`crawfish-*` / `craw-*`
prefix** (RFC O-4) so it never collides with per-Definition subagent files emitted by
`craw export --claude-code`.

```text
packages/crawfish/src/crawfish/plugin/
├── .claude-plugin/
│   └── plugin.json                     # manifest (semver version — MUST bump for updates)
├── skills/
│   ├── crawfish-security-spine/
│   │   └── SKILL.md                    # CRA-248 — load-bearing
│   ├── crawfish-pipeline-model/
│   │   └── SKILL.md                    # CRA-249
│   ├── crawfish-determinism-ledger/
│   │   └── SKILL.md                    # CRA-250
│   ├── crawfish-authoring/             # M3a authoring playbook (CRA-256..264)
│   │   ├── SKILL.md                    # router/index skill (user-invocable:false)
│   │   ├── definition-py.md           # CRA-258 reference (sibling, progressive disclosure)
│   │   ├── instructions-agents.md     # CRA-259
│   │   ├── tools-py.md                # CRA-260
│   │   ├── mcp-py.md                  # CRA-261
│   │   ├── policies-skills.md         # CRA-262
│   │   ├── knowledge.md              # CRA-263
│   │   ├── fixtures-evals.md         # CRA-264
│   │   └── optimizing-a-component.md # UNFILED-OPT
│   └── crawfish-explain/
│       └── SKILL.md                    # CRA-251 / explain index (reads shipped docs)
├── commands/                           # legacy slash commands — thin `craw code …` wrappers
│   ├── craw-init.md                    # CRA-251
│   ├── craw-new.md
│   ├── craw-sync.md
│   ├── craw-map.md
│   ├── craw-describe.md
│   └── craw-eval.md
└── pyproject-fragment.toml             # `[tool.crawfish.plugin]` pin metadata (UNFILED-PIN)
```

`.claude-plugin/plugin.json` (grounded in CC manifest fields — `version` is semver and
**must be bumped** for users to get updates; `defaultEnabled:false` keeps it opt-in until
`craw code init` enables it for the project):

```json
{
  "name": "crawfish",
  "description": "Knowledge and command ergonomics for authoring & operating a Crawfish project: the security spine, the pipeline mental model, determinism discipline, and the file-by-file definition authoring playbook.",
  "version": "0.2.0",
  "author": { "name": "Crawfish", "url": "https://github.com/crawfish-dev/crawfish" },
  "defaultEnabled": false,
  "dependencies": {},
  "metadata": {
    "requires_crawfish": ">=0.2,<0.3",
    "bundle_sha256": "<filled by `craw code build-plugin`; verified by craw doctor>"
  }
}
```

Two lockfiles, kept distinct (CC platform fact: **there is no `claude.lock`**; CC resolves
plugin deps from the manifest's semver ranges). Crawfish's **framework** lockfile
`crawfish.lock` pins the plugin **bundle** by content digest (`bundle_sha256`) and the
`requires_crawfish` compat range (UNFILED-PIN); CC's own manifest-based dependency
resolution is orthogonal and untouched.

---

## M2 — Scaffolding

All M2 verbs live under a new `craw code` argparse subtree
(`packages/crawfish/src/crawfish/code/cli.py`, registered from the existing
`crawfish/cli.py` `sub.add_parser("code", …)`). Each composes existing machinery:
`crawfish.scaffold` (FILES/`scaffold_project`), `crawfish.discovery`, `crawfish.doctor`,
`crawfish.provenance`, `crawfish.secrets`. Every verb takes the shared flags `--json`
(versioned `schema` key), `--org`, and emits the `craw.error.v1` envelope from spec 01 on
failure.

### CRA-245 — `craw code init <dir>`
**Milestone:** M2 · **Priority:** Urgent · **Depends on:** CRA-279 (re-entrancy), M1 `--json`/error envelope, UNFILED-PIN (plugin pin)
**Context** — A bare `craw init` (`crawfish/scaffold.py`, `scaffold_project`) writes the
authored tree but does nothing about the agent loop: no plugin install, no ledger start,
no consent record. RFC §4.1 says `craw code init` must do *more* — scaffold the canonical
layout **and** install the `crawfish-*` plugin bundle, start the `.crawfish/` ledger the
dashboard reads, and print next steps. It must never reach a live model or resolve a secret.
**Design** — `init` runs three composable steps behind one verb:
1. **Scaffold** — reuse `crawfish.scaffold.scaffold_project` to write the seven authored
   folders + `crawfish.toml`, plus the secrets-by-reference templates from CRA-276. The
   triage-bot hero example is the seed.
2. **Install plugin** — copy `crawfish/plugin/` into `<dir>/.claude/plugins/crawfish/`
   (disjoint from `.claude/agents/` that export owns — RFC O-4), enable it in
   `<dir>/.claude/settings.json` (`enabledPlugins`), and **pin** the bundle: write the
   `bundle_sha256` + `requires_crawfish` range into `crawfish.lock` (UNFILED-PIN). The
   `.claude` tree is already excluded from the Definition `sha` (`definition.md` exclusion
   list), so this never perturbs content identity.
3. **Start the ledger** — create `<dir>/.crawfish/` and open the `SqliteStore` (WAL)
   through the `Store` protocol only — **never** import `SqliteStore` in the verb; construct
   it behind the existing store factory. Record an init provenance row
   (`crawfish.provenance.record_provenance`, `generated_by="craw-code-init"`,
   `source_tainted=False`). Do **not** seed runs.

`init` is idempotent by delegating reconcile semantics to CRA-279 (re-running never
clobbers the ledger). The guided first-run flow (UNFILED-ADOPT) is printed as next-steps
text, not executed.
**Interface**
```text
craw code init [<dir>]                 # default: cwd
  --name NAME        project name (crawfish.toml [project].name)
  --no-plugin        scaffold + ledger only, skip plugin install
  --upgrade          re-pin plugin + reconcile (see CRA-279); never overwrites authored files
  --org ID           tenancy (default "local")
  --json             emit craw.code.init.v1
exit: 0 ok · 2 dirty_init (tampered .crawfish/) · 3 consent declined · 4 usage
```
```jsonc
// craw.code.init.v1
{
  "schema": "craw.code.init.v1",
  "project": "crawfish-app",
  "dir": "/abs/path",
  "scaffolded": ["crawfish.toml", "definitions/triage-bot/…", "…"],  // created only
  "skipped_existing": ["crawfish.toml"],                              // reconciled, untouched
  "plugin": { "installed": true, "name": "crawfish", "version": "0.2.0",
              "bundle_sha256": "…", "requires_crawfish": ">=0.2,<0.3" },
  "ledger": { "started": true, "path": ".crawfish/" },
  "next_steps": ["craw code new definition my-agent", "craw dev definitions/triage-bot -i …"]
}
```
**Acceptance criteria**
- [ ] `craw code init x` on an empty dir writes the canonical seven folders + `crawfish.toml` + secrets-by-ref templates, installs the plugin under `.claude/plugins/crawfish/`, and opens `.crawfish/`.
- [ ] The verb never imports `SqliteStore` directly; the store is reached through the protocol/factory (grep test asserts no concrete-backend import in `code/`).
- [ ] No live model call and no secret resolution occurs (assert via a no-network harness).
- [ ] Plugin bundle digest + `requires_crawfish` range are written to `crawfish.lock`.
- [ ] `--json` matches the snapshot `craw.code.init.v1`.
**Test plan** — `packages/crawfish/tests/test_code_init.py`: empty-dir scaffold; `--no-plugin` path; `.claude/` namespace disjoint from any `agents/` export; `crawfish.lock` pin written; `--json` snapshot. All under `MockRuntime`, tmp dirs, no network.
**Security review notes** — Rules 4 & 6 (`SECURITY.md`): templates are reference-only (CRA-276); plugin bundle is pinned/digested (supply chain). No fluid surface touched. `init` resolves no secret, so nothing to scrub.

### CRA-246 — `craw code new <kind> <name>` templates
**Milestone:** M2 · **Priority:** High · **Depends on:** CRA-245, CRA-276
**Context** — Standing up each component by hand is the bootstrap tax RFC §1 calls out.
`craw code new` authors a new component from a template into the **correct folder** per the
canonical layout (`docs/guide/project-structure.md`), respecting `[project.paths]`
overrides so a relocated tree still lands files in the right place.
**Design** — A `TEMPLATES: dict[kind, dict[relpath, str]]` table in
`crawfish/code/templates.py`, mirroring `crawfish.scaffold.FILES`. `kind ∈ {definition,
pipeline, source, sink, tool, observer, policy, mcp}`. Resolve the destination folder via
`crawfish.config` `[project.paths]` (falling back to canonical defaults), refuse to
overwrite an existing path (fail-closed, exit 5), and after writing run a **secret-shaped
lint** (CRA-276) over the emitted files. Every template models the **static/fluid spine**:
a `definition` template marks consequential config `Flow.STATIC` and untrusted data
`Flow.FLUID`; an `mcp` template uses `auth="<ENV_VAR_NAME>"` (reference only). The new
component is loadable: `craw code new definition foo` then `craw code sync` must pass.
**Interface**
```text
craw code new <kind> <name> [--dir .] [--force] [--json]
  kinds: definition pipeline source sink tool observer policy mcp
exit: 0 ok · 5 exists (refused) · 6 secret_shaped_lint_failed · 4 usage
```
Template emitted for `craw code new mcp github` → `mcp/github.py` (callable/instance name = filename stem; auth by reference only — `definition.md` MCP rule):
```python
"""GitHub MCP connection. Auth is a SECRET REFERENCE — an env-var name, never a value."""
from __future__ import annotations
from crawfish.definition.types import MCPConnection

github = MCPConnection(
    name="github",
    description="GitHub issues/PRs server.",
    command=["npx", "-y", "@modelcontextprotocol/server-github"],
    auth="GITHUB_TOKEN",          # ← reference only; resolved into server env at run time
    tools=["create_issue", "search_issues"],
)
```
Template emitted for `craw code new definition triage` → `definitions/triage/definition.py`:
```python
"""Typed IO. STATIC = author config (a sink target, a project id). FLUID = untrusted data."""
from __future__ import annotations
from crawfish.core import Flow, Parameter

inputs = [
    Parameter(name="project", type="str", flow=Flow.STATIC),   # set once at batch start
    Parameter(name="ticket_body", type="str"),                 # default fluid → untrusted
]
outputs = [Parameter(name="triage", type="str")]
lead = "lead"
```
`craw.code.new.v1` JSON: `{ "schema": "craw.code.new.v1", "kind": "...", "name": "...",
"written": ["mcp/github.py"], "folder": "mcp/", "lint": { "secret_shaped": "clean" } }`.
**Acceptance criteria**
- [ ] Each `kind` emits to its canonical folder, honoring `[project.paths]` overrides.
- [ ] An `mcp` template uses `auth="<ENV_VAR>"`; the `definition` template marks at least one `Flow.STATIC` and one default-fluid `Parameter`.
- [ ] Refusing to overwrite an existing path exits 5; `--force` overwrites only with provenance recorded.
- [ ] A freshly `new`-ed component passes `craw code sync` (loads clean).
- [ ] Secret-shaped lint runs post-write; a template that fails it would block (CRA-276).
**Test plan** — `packages/crawfish/tests/test_code_new.py`: one case per kind asserting destination + content; relocation via `[project.paths]`; overwrite refusal; emitted `mcp` template loads with `load_definition` when placed in a definition dir.
**Security review notes** — Rules 1, 2, 4. Templates teach the *correct shape*: reference-only auth, static-only consequential slots. Red-team N/A (no runtime fluid surface), but the lint (CRA-276) is the enforcement that a generated template never models an inline secret.

### CRA-247 — `craw code sync`
**Milestone:** M2 · **Priority:** High · **Depends on:** CRA-245, M1 assembly-gate-in-run (CRA-272), CRA-278 (tree lock)
**Context** — The self-generating loop (RFC §5) edits files and immediately calls into the
project; the agent needs a "where am I / is the tree healthy" call. `craw code sync`
composes `craw doctor` + `craw list` (`docs/guide/cli.md`) into one reconciliation that
reports drift between the authored tree and discovery — and, critically, runs the
**assembly gate** as a precondition so the edit→run loop can't skip it.
**Design** — Compose three reads: (1) `crawfish.discovery` to enumerate components, (2)
`crawfish.doctor` for structure health (misplaced files, authored-vs-generated tamper),
(3) per-Definition `load_definition` to surface `DefinitionLoadError`s as structured
findings. For any Definition touched since last sync, invoke the assembly gate
(`assert_build_safe` — verify name; the fluid→static-sink check, `SECURITY.md` rule 8)
**before** declaring the tree runnable, under the CRA-278 advisory read lock so a
half-written file is never compiled. `sync` reads only the filesystem + Store; it never
runs a model or resolves a secret (mirrors `craw doctor`'s safety contract,
`docs/guide/project-structure.md`).
**Interface**
```text
craw code sync [--dir .] [--org ID] [--json]
exit: 0 clean · 1 drift/warnings · 2 dirty (.crawfish tamper) · 7 assembly_gate_rejected · 8 tree_busy
```
```jsonc
// craw.code.sync.v1
{ "schema": "craw.code.sync.v1",
  "components": { "definitions": ["triage-bot"], "pipelines": ["main"], "sources": [], "sinks": [] },
  "drift": [ { "kind": "misplaced", "path": "tools/format.py", "hint": "looks like a Definition → definitions/" } ],
  "load_errors": [ { "component": "definitions/foo", "code": "DefinitionLoadError", "message": "unknown tool 'search'" } ],
  "assembly_gate": { "checked": ["triage-bot"], "rejected": [] },
  "ledger": "clean" }
```
**Acceptance criteria**
- [ ] Reports misplaced files, stray Python, and `.crawfish/` tamper exactly as `craw doctor` does, in structured form.
- [ ] A Definition with an unknown tool/policy/role binding surfaces a `DefinitionLoadError` finding, exit 1, not a crash.
- [ ] The assembly gate runs on touched Definitions; a fluid→static-sink wiring yields exit 7 with a `craw.error.v1`-shaped finding (`retryable:false`).
- [ ] No model call, no secret resolution.
- [ ] Concurrent edit during sync yields `tree_busy` (exit 8) via the CRA-278 lock, never a torn compile.
**Test plan** — `packages/crawfish/tests/test_code_sync.py`: clean tree; misplaced-file drift; load-error surfacing; assembly-gate rejection on an injected fluid→sink wiring; tamper detection; `--json` snapshot.
**Security review notes** — Rules 5 & 8. The assembly-gate precondition is the load-bearing enforcement here: it closes the §12.2 gap "assembly gate skipped in the edit→run loop." Red-team: a Definition whose pipeline routes `ticket_body` (fluid) into a sink `target` must be rejected at sync, exit 7.

### CRA-276 — Secrets-by-reference-only scaffolding templates + secret-shaped lint
**Milestone:** M2 · **Priority:** Medium · **Depends on:** none (consumed by CRA-245/246)
**Context** — Templates teach shape. A template that models an inline secret or a fluid
destination teaches Claude the *wrong* shape, and an injected agent will copy it (RFC §12.2).
Mitigation: reference-only credential slots, static-only destinations, and a
secret-shaped-literal lint that fails closed (§12.2 table).
**Design** — Two parts. (a) **Template hygiene**: every credential slot is an env-var
*name* (`auth="GITHUB_TOKEN"`, `MCPConnection.auth` per `definition.md`), every
consequential destination is a `Flow.STATIC` parameter or `crawfish.toml [capabilities]`
entry, and `.env.example` documents references-only (matching `crawfish.scaffold` today,
lines 41–42). (b) **`secret_shaped` lint** in `crawfish/code/lint.py`: a pure, AST-/regex-
based scan over emitted/edited Python and markdown for high-entropy literals and
known-credential shapes (`ghp_…`, `sk-…`, AWS `AKIA…`, JWT, base64 ≥32 chars assigned to a
`token|secret|password|key|auth` name). A hit fails closed (exit 6) with a remediation
pointing at reference-by-name. Reuse the `ScrubbingStore` redaction patterns
(`crawfish.store`/`crawfish.secrets`) as the shared detector so lint and scrub agree.
**Interface**
```text
craw code lint [--dir .] [--fix-hint] [--json]    # standalone; also run inside `new`
exit: 0 clean · 6 secret_shaped_literal_found
```
```jsonc
// craw.code.lint.v1
{ "schema": "craw.code.lint.v1",
  "findings": [ { "path": "mcp/x.py", "line": 7, "kind": "inline_secret",
                  "match_redacted": "ghp_…REDACTED", "remediation": "reference by env-var name: auth=\"GITHUB_TOKEN\"" } ],
  "verdict": "fail" }
```
**Acceptance criteria**
- [ ] Every shipped template (CRA-245/246) passes `secret_shaped` lint and contains no inline credential.
- [ ] The lint flags each shape class (GitHub PAT, OpenAI key, AWS key, JWT, generic high-entropy assigned to a secret-named var) with the value **redacted** in output.
- [ ] The lint is pure (no network, no model) and shares its detector with `ScrubbingStore` redaction.
- [ ] A hit fails closed: exit 6, remediation names reference-by-name.
**Test plan** — `packages/crawfish/tests/test_code_lint.py`: positive cases per shape; negative cases (env-var refs, comments, `.env.example`); assert output value is redacted, never echoed raw; assert detector parity with `ScrubbingStore`.
**Security review notes** — Rule 4. The lint output must itself be scrubbed (never echo the matched secret) — a finding that printed the literal would be a leak. Detector parity with the scrub path prevents a template passing lint but tripping scrub at runtime.

### CRA-277 — Re-enter the consent gate for agent-added MCP servers and deps
**Milestone:** M2 · **Priority:** High · **Depends on:** CRA-245, `crawfish.provenance.regate_generated`
**Context** — When Claude (possibly steered by fluid data) authors a new `MCPConnection`
(`mcp/*.py`) or a new `DefinitionRef` dependency, it adds a capability (egress + a secret
reference) that **bypasses the install-time consent gate** (§12.2). The framework already
has the enforcement seam: `crawfish.provenance.regate_generated` diffs newly-declared
capabilities against the prior `Grant` and re-enters `secrets.consent_install`, fail-closed
via `DenyConsent`.
**Design** — On `craw code sync`/`new`/any write that adds an `MCPConnection` or
`DefinitionRef`, compute `declared_capabilities(definition)` (static-only — a fluid value
can never name a secret/egress, per `provenance.declared_capabilities`) and call
`regate_generated(definition, store=…, generated_by="craw-code",
source_tainted=<true if the authoring drew on fluid input>, decider=…)`. In an
interactive terminal the decider is `CallbackConsent` over a stdin prompt
(references-only `ConsentRequest.summary()`); non-interactive (the agent loop) defaults to
`DenyConsent` → `ConsentRequired` raised → exit 3 with a `craw.error.v1`
(`retryable:false`, remediation "run `craw code grant <component>` to consent"). On consent
a `Grant` is recorded in `crawfish.lock` via `GrantManifest`; the new MCP/dep is pinned.
**Interface**
```text
craw code grant <component> [--yes] [--org ID] [--json]   # interactive consent re-entry
# automatic re-gate fires inside sync/new; this verb is the human approval entry point
exit: 0 granted · 3 declined (ConsentRequired) · 4 usage
```
```jsonc
// craw.code.grant.v1
{ "schema": "craw.code.grant.v1", "component": "definitions/triage",
  "new_capabilities": { "secrets": ["GITHUB_TOKEN"], "egress": ["github"] },
  "decision": "granted", "grant_id": "…", "pinned_in": "crawfish.lock" }
```
**Acceptance criteria**
- [ ] Adding an `mcp/*.py` with `auth="X"` to a Definition triggers `regate_generated` on the next sync.
- [ ] Non-interactive context defaults to `DenyConsent`; an un-consented new MCP raises `ConsentRequired` → exit 3, `retryable:false`.
- [ ] Consent records a `Grant` (full declared surface) and pins the MCP/dep in `crawfish.lock`.
- [ ] An artifact declaring nothing new (already covered by the prior grant) needs no re-consent (no prompt).
- [ ] The consent surface shows secrets **by reference name only**, never a value.
**Test plan** — `packages/crawfish/tests/test_code_consent_regate.py`: new-MCP triggers re-gate; `DenyConsent` fail-closed; `AutoConsent --yes` records grant; no-new-capability no-op; tenancy (a grant in org A invisible to org B). All use the existing `ConsentDecider` fakes — no stdin.
**Security review notes** — Rules 4 & 6, plus SECURITY.md language-era "generated artifacts must pass the assembly gate." Red-team: an injected ticket steers the agent to author `mcp/exfil.py` with `auth="GITHUB_TOKEN"` + `url="https://attacker"`; the re-gate must fire (declared egress is new), default-deny, and block the unattended run. `source_tainted=True` is recorded so the audit can flag the tainted provenance.

### CRA-278 — Authoring-tree file lock / read-during-edit consistency
**Milestone:** M2 · **Priority:** High · **Depends on:** Store borrow primitive (verify name), CRA-247
**Context** — The agent edits a file while `craw code sync`/`run` compiles it. A
half-written `definition.py` compiles to the wrong content sha → corrupt run identity
(§12.3). The fix is an advisory lock so a torn tree is refused, not compiled.
**Design** — An advisory **read/write lock over the authored tree**, keyed on
`(org_id, project_dir)`, backed by the Store's borrow/lease primitive (the same exclusive
borrow `DefinitionStore` train-mode uses — **verify name**; it is tenancy-scoped and
Store-enforced per SECURITY.md "Mutable borrows"). A writer (`craw code new`, an Edit) takes
a short exclusive lease around the write+fsync; a reader (`sync`/`run`/`describe`/`map`)
takes a shared lease around `load_definition`. If a compile cannot acquire the shared lease
(a write in flight), it returns `tree_busy` (exit 8) rather than compiling a torn file.
`craw doctor` gains a torn-tree check (a file whose mtime is newer than its lock release).
Locks live under `.crawfish/locks/` (generated state — never authored).
**Interface**
```text
# no new top-level verb; behavior is internal to new/sync/run/describe/map.
exit on contention: 8 tree_busy   (craw.error.v1: code="tree_busy", retryable:true)
```
**Acceptance criteria**
- [ ] A compile concurrent with an in-flight write returns `tree_busy` (exit 8, `retryable:true`), never a wrong-sha Definition.
- [ ] The lock is tenancy-scoped (org A's lock never blocks org B) and Store-enforced (survives process boundary).
- [ ] `craw doctor` flags a torn tree (mtime newer than last lock release).
- [ ] The lock record lives under `.crawfish/locks/`, excluded from the Definition sha.
**Test plan** — `packages/crawfish/tests/test_code_tree_lock.py`: simulate writer-holds-lease → reader gets `tree_busy`; two-org isolation; release→re-acquire; torn-tree doctor check. Deterministic, no real concurrency races (drive the lease primitive directly).
**Security review notes** — Run-identity integrity (SECURITY.md "no decode field escapes run identity" sibling). A torn compile producing a stable-looking but wrong sha would let an unreviewed edit ride a previously-signed sha — the lock prevents that. Reuses the tenancy-scoped Store lock, so no new trust surface.

### CRA-279 — Make `craw code init` idempotent and re-entrant (`--upgrade`)
**Milestone:** M2 · **Priority:** High · **Depends on:** CRA-245
**Context** — `craw code init` re-run must never clobber an existing project or, worse, the
ledger/registry under `.crawfish/` (§12.3). It must reconcile-not-overwrite, leave machine
state alone, and offer `--upgrade` to re-pin the plugin.
**Design** — `init` becomes reconcile-first: for each scaffold path, **create only if
absent**; record skipped (existing) paths in the JSON output (`skipped_existing`). **Never
touch** `.crawfish/` contents — if it exists, open it; if a generated artifact is tampered
(`craw doctor` authored-vs-generated check), refuse with `dirty_init` (exit 2). `--upgrade`
re-runs the plugin install step only (re-copy `crawfish/plugin/`, recompute `bundle_sha256`,
re-pin `requires_crawfish` in `crawfish.lock`) and reconciles any newly-added canonical
folders; it never rewrites authored component files. Provenance for the upgrade is recorded
(`generated_by="craw-code-init-upgrade"`).
**Interface**
```text
craw code init [<dir>] [--upgrade] [--json]
exit: 0 ok · 2 dirty_init (tampered .crawfish) · 4 usage
```
```jsonc
// craw.code.init.v1 (re-entrant fields)
{ "schema": "craw.code.init.v1", "scaffolded": [], "skipped_existing": ["crawfish.toml","definitions/triage-bot/instructions.md"],
  "ledger": { "started": false, "preserved": true },
  "plugin": { "upgraded": true, "from": "0.1.0", "to": "0.2.0", "bundle_sha256": "…" } }
```
**Acceptance criteria**
- [ ] Re-running `init` on an existing project creates nothing, lists `skipped_existing`, and leaves `.crawfish/` byte-identical.
- [ ] A tampered `.crawfish/` artifact yields `dirty_init` (exit 2), no writes.
- [ ] `--upgrade` re-pins the plugin in `crawfish.lock` and reconciles new folders without rewriting authored files.
- [ ] Ledger rows present before init are present and unchanged after.
**Test plan** — `packages/crawfish/tests/test_code_init_reentrant.py`: scaffold → seed a fake ledger row → re-init → assert row intact + nothing overwritten; tamper detection; `--upgrade` re-pin; snapshot. No network/model.
**Security review notes** — Rule 6 + authored-vs-generated boundary (`project-structure.md`). Refusing to overwrite the ledger prevents an attacker (or a confused agent) from resetting the audit trail by re-running init. `dirty_init` fail-closed mirrors `craw doctor`'s tamper stance.

### UNFILED-MAP — `craw code map` (whole-project discovery graph)
**Milestone:** M2 · **Priority:** High · **Depends on:** CRA-247, `crawfish.discovery`, CRA-271 (`describe` redaction)
**Context** — The agent needs a single, never-stale "what's in this project and how does it
wire together" call (RFC §12.4, CRA cap). `map` emits the component graph: flow-tagged IO,
pipeline topology, consequential sinks, deployed supervisors, and version lineage — the
orientation read before any authoring move.
**Design** — A pure reflection over `crawfish.discovery` + per-Definition `load_definition`
+ the Store (via the `ObserverSurface`/`Store` protocol for lineage and deploy registry —
**never** import `SqliteStore`). Each node carries `kind`, `id`, typed IO with `flow`
tags (`Parameter.flow`), and edges (`pipeline` wiring source→batch→aggregator→sink,
`DefinitionRef` deps, `with_context` summon pins). **Consequential sinks are flagged**;
their `target` is shown as *static-only kind*, never a resolved destination (route through
the same redaction projection as `craw code describe`, CRA-271 — surface capability *kind*,
not destination). Cached by content sha under `.crawfish/` (mirrors CRA-274's `describe`
cache) so a large project's `map` is cheap; the cache is reconciled on `sync`.
**Interface**
```text
craw code map [--dir .] [--org ID] [--json] [--format {json,dot}]
exit: 0 ok · 2 dirty · 4 usage
```
```jsonc
// craw.code.map.v1
{ "schema": "craw.code.map.v1",
  "nodes": [
    { "kind": "definition", "id": "triage-bot",
      "inputs": [ {"name":"project","type":"str","flow":"static"}, {"name":"ticket_body","type":"str","flow":"fluid"} ],
      "outputs": [ {"name":"triage","type":"str"} ] },
    { "kind": "sink", "id": "github-issues", "consequential": true, "target_kind": "static", "egress_kind": "github" }
  ],
  "edges": [ { "from": "source:tickets", "to": "definition:triage-bot", "via": "batch" },
             { "from": "definition:triage-bot", "to": "sink:github-issues" } ],
  "lineage": { "triage-bot": ["sha-a","sha-b"] },
  "deployed": [] }
```
**Acceptance criteria**
- [ ] Emits every discovered component with flow-tagged typed IO and pipeline/dep/summon edges.
- [ ] Consequential sinks are flagged; their target appears as a static-only **kind**, never a resolved destination or secret.
- [ ] Store data flows through the `ObserverSurface`/`Store` protocol; no concrete-backend import (grep test).
- [ ] `--format dot` renders a graphviz graph from the same model.
- [ ] Cached by content sha; an unchanged project re-maps from cache.
**Test plan** — `packages/crawfish/tests/test_code_map.py`: fixture project → assert nodes/edges/flow tags; consequential-sink redaction (no destination leaks); cache hit on unchanged sha; two-org isolation. `MockRuntime`, no network.
**Security review notes** — Rules 2 & 4 + the M1 `describe`-redaction discipline (CRA-271). `map` is a high-value leak surface: it must surface capability *kind*, not egress hosts/sink destinations/secret refs. Red-team: a Definition with `auth="SECRET"` and `url="https://internal.host"` must render `egress_kind` only, never the URL or the env-var value.

### UNFILED-ADOPT — `craw code adopt` (+ guided first-run + `craw code explain`)
**Milestone:** M2 · **Priority:** Medium · **Depends on:** CRA-245, UNFILED-MAP, UNFILED-O4 (ADR), `craw export --claude-code`
**Context** — An existing Crawfish project (authored before `craw code`, or via `craw init`)
needs to be brought into the agent loop without re-scaffolding: install the plugin/ledger,
`map` it, validate it loads clean, and start the guided first-run. RFC O-4 (resolved):
`adopt` **subsumes `craw export --claude-code`** as its plugin-install step, with disjoint
`.claude/` namespaces.
**Design** — `adopt` runs: (1) detect an existing project (`crawfish.toml` present), (2)
install the `crawfish-*` plugin + start `.crawfish/` **only if absent** (reuse CRA-279
reconcile), (3) run `craw export --claude-code` for each Definition to emit per-Definition
subagent files under `.claude/agents/` (export's namespace) — **disjoint** from the plugin's
`.claude/plugins/crawfish/` namespace (O-4; preserves the `.claude`-excluded-from-`sha`
invariant), (4) run `craw code map` + `craw code sync` to validate and report, (5) print the
guided first-run (a next-step recipe: pick a Definition → `craw dev` on the mock → read the
ledger). `craw code explain <topic>` is a thin reader over the shipped plugin skills/docs
(`crawfish/plugin/skills/**` + `docs/`), surfacing a topic's body to the user — no model
call, just file retrieval.
**Interface**
```text
craw code adopt [<dir>] [--no-export] [--org ID] [--json]
craw code explain <topic>   # topics: security-spine, pipeline-model, determinism, definition-py, …
exit: 0 ok · 2 dirty · 9 not_a_project · 4 usage
```
```jsonc
// craw.code.adopt.v1
{ "schema": "craw.code.adopt.v1", "dir": "/abs", "plugin": {"installed": true},
  "exported": [ { "definition": "triage-bot", "file": ".claude/agents/triage-bot.md" } ],
  "map": { "nodes": 4, "consequential_sinks": 1 },
  "validation": { "sync": "clean" },
  "next_steps": ["craw dev definitions/triage-bot -i project=acme -i ticket_body=…"] }
```
**Acceptance criteria**
- [ ] `adopt` on an existing project installs the plugin + ledger without overwriting authored files (reconcile via CRA-279).
- [ ] Export writes per-Definition subagents under `.claude/agents/` (export namespace); the plugin lives under `.claude/plugins/crawfish/` — namespaces disjoint, both excluded from sha.
- [ ] Exported files carry no secrets (export invariant, `claude-code-export.md`).
- [ ] `adopt` runs `map` + `sync` and reports validation; a load error surfaces, not crashes.
- [ ] `craw code explain <topic>` returns the matching skill/doc body with no model call.
**Test plan** — `packages/crawfish/tests/test_code_adopt.py`: pre-existing project → adopt → assert plugin + export coexist, ledger preserved, validation reported; `not_a_project` exit 9; `explain` topic retrieval. `packages/crawfish/tests/test_code_explain.py` for topic→file mapping.
**Security review notes** — O-4 namespace disjointness preserves the `.claude`-excluded-from-`sha` invariant (`definition.md` exclusion list) so adopt never perturbs content identity. Export carries no secrets (SECURITY.md "operate and observe" §). Validation re-runs the assembly gate (via `sync`) on the adopted tree, so an adopted project with a fluid→sink wiring is flagged at adoption.

---

## M3 — Plugin (skills + commands)

The plugin teaches the four knowledge areas RFC §8 enumerates. Each is a CC skill: YAML
frontmatter (`name`, `description` — which drives autonomous invocation — `allowed-tools`,
and `disable-model-invocation`/`user-invocable` where relevant) over a markdown body kept
under 500 lines, with reference material in sibling files (progressive disclosure). The
security-spine skill is **load-bearing** but is a *guideline* — the enforcement lives in
the M2 verbs (consent re-gate, assembly gate, lint) and M0 (jailed compile), per RFC §12.1
("a skill is a guideline an injected agent can be talked out of").

### CRA-248 — Plugin skill: the security spine (load-bearing)
**Milestone:** M3 · **Priority:** Urgent · **Depends on:** UNFILED-PIN (bundle pin)
**Context** — RFC §8: if the spine isn't *taught*, Claude will wire a fluid input into a
sink target — a prompt-injection vector. This skill makes the six core rules + the
static/fluid boundary impossible to miss, and points the agent at the verbs that *enforce*
them so it never relies on memory alone.
**Design** — A high-`description`-salience skill that auto-loads its summary at session
start (CC progressive disclosure) so the boundary is always in context. Body: the
prompt-injection boundary, the static-vs-fluid table, "consequential sink targets &
idempotency keys are static-only," "secrets resolve by reference, never in-prompt," and a
**checklist the agent runs before authoring any sink/MCP/policy**. It explicitly cites the
enforcing verbs (`craw code sync` assembly gate, `craw code grant` consent re-gate, the
secret-shaped lint). `allowed-tools: Read, Grep` only (knowledge skill, no side effects).
**Interface**
```yaml
---
name: crawfish-security-spine
description: >
  The Crawfish security spine — the prompt-injection boundary. Load whenever authoring
  or wiring a Definition, sink, MCP connection, policy, or pipeline. Fluid inputs are
  untrusted data and never reach a sink target or an instruction slot; consequential
  sink targets and idempotency keys are static-only; secrets resolve by reference.
allowed-tools: Read, Grep
---
```
Body outline: (1) The boundary in one sentence. (2) Static vs fluid — the table + how
`Parameter.flow` encodes it. (3) The six core rules (verbatim from SECURITY.md) +
language-era rules 7–9 (eval-mode sinks, fluid→static rejected at assembly, aggregate taint
is union). (4) **Never do this**: wire `Flow.FLUID` → a sink `target`, put a secret value in
a prompt/config, hand-write `.crawfish/`. (5) The pre-authoring checklist. (6) Enforcement
pointers: `craw code sync` (assembly gate), `craw code grant` (consent), `craw code lint`
(secret-shaped).
**Acceptance criteria**
- [ ] Frontmatter `description` names the boundary and triggers on sink/MCP/policy/pipeline authoring.
- [ ] Body cites all six core rules + rules 7–9 and the static-only sink/idempotency invariant.
- [ ] Body points to the enforcing verbs (not just the rules).
- [ ] Skill is `allowed-tools: Read, Grep` (no write/exec); body < 500 lines.
- [ ] The skill file is included in the pinned plugin bundle (UNFILED-PIN digest).
**Test plan** — `packages/crawfish/tests/test_plugin_skills.py`: parse frontmatter (valid YAML, required keys); assert the body contains each core-rule phrase and the enforcing-verb references; assert `allowed-tools` has no `Write`/`Bash`; assert <500 lines.
**Security review notes** — This skill *is* the teaching half of the spine; the spec is explicit that it is not the enforcement (M2/M0 are). Bundle pin (UNFILED-PIN) is what keeps the rules' source from being silently swapped.

### CRA-249 — Plugin skill: pipeline mental model
**Milestone:** M3 · **Priority:** High · **Depends on:** none
**Context** — RFC §8: Claude must know what a Definition/pipeline is and *when to reach for
batch fan-out vs an aggregator vs a refine loop*. Without it the agent builds unidiomatic
topologies.
**Design** — A skill teaching `Source → Filter → Batch (fan-out) → Aggregator (reduce) →
Router (branch) → Sink`, the Definition directory contract (`definition.md`), and a
decision guide: fan-out for per-item independent work, aggregator to reduce, router to
branch on a (tainted, non-sink-choosing) label, refine loop to iterate to a goal/bound
(`craw refine --until`). Sibling reference file for the full directory contract.
**Interface**
```yaml
---
name: crawfish-pipeline-model
description: >
  The Crawfish pipeline mental model — Source → Filter → Batch → Aggregator → Router →
  Sink, and the Definition directory contract. Load when designing a pipeline or deciding
  between batch fan-out, an aggregator reduce, a router branch, or a refine loop.
allowed-tools: Read, Grep
---
```
Body outline: (1) The pipeline shape + each node's job. (2) The Definition directory
(`instructions.md`, `agents/*.md`, `tools/*.py`, `mcp/*.py`, `policies/*.py`,
`definition.py`) — one-line each, link to the authoring playbook. (3) Decision guide:
fan-out vs aggregator vs router vs refine, with a worked "triage many tickets" example. (4)
Coordination shapes (`single`/`lead`/`sequential`).
**Acceptance criteria**
- [ ] Body covers all six pipeline node kinds and the Definition directory contract.
- [ ] Includes the fan-out/aggregator/router/refine decision guide with a worked example.
- [ ] Links to the M3a authoring playbook skills.
- [ ] Frontmatter valid; body < 500 lines; `allowed-tools: Read, Grep`.
**Test plan** — `test_plugin_skills.py` (shared): frontmatter parse; assert node kinds + coordination shapes present; assert links to authoring playbook resolve to shipped files.
**Security review notes** — Teaching the router rule (a label may gate *whether* a consequential action fires, never *choose among* sinks — SECURITY.md language-era table) belongs here so the topology is idiomatic *and* safe.

### CRA-250 — Plugin skill: determinism + reading the ledger
**Milestone:** M3 · **Priority:** High · **Depends on:** none
**Context** — RFC §8: mock-by-default, `--seed` carries all randomness, `--live` is explicit
and budgeted; and the agent must interpret `craw inspect`/`craw logs`/`--json` to pick the
next move.
**Design** — A skill teaching the determinism discipline (iterate on `craw dev`/mock,
promote to `--live --budget` deliberately) and how to read the ledger: the `--json`
integration surface (`craw.<cmd>.v<N>`), `craw inspect <run>`/`craw logs <run>`, the cost
band (`total_usd`/`expected_usd`/`worst_case_usd`), and `craw.error.v1` recovery
(`retryable` decides retry-vs-stop; security rejections are `retryable:false`). Sibling file
with the `--json` schema index.
**Interface**
```yaml
---
name: crawfish-determinism-ledger
description: >
  Determinism discipline and reading the ledger in Crawfish. Load before running,
  evaluating, or debugging a component: iterate on the mock (craw dev), carry randomness
  in --seed, promote to --live under --budget; read results via craw inspect / craw logs
  and the versioned --json surface.
allowed-tools: Read, Grep, Bash
---
```
Body outline: (1) Mock-by-default; `--seed`; `--live --budget` is deliberate. (2) The
`--json` contract (versioned `schema`, snapshot-tested) and where to parse the cost band.
(3) `craw inspect`/`craw logs`/`craw code map` for "what happened / where am I." (4)
`craw.error.v1` — `retryable` drives the next move; `retryable:false` = stop (a security
rejection). `allowed-tools` includes `Bash` because reading the ledger is a `craw … --json`
call (the RFC's CLI-as-contract).
**Acceptance criteria**
- [ ] Body teaches mock→`--seed`→`--live --budget` promotion and the `--json` cost band.
- [ ] Body teaches `craw.error.v1` `retryable` semantics (security = `retryable:false`).
- [ ] `allowed-tools` includes `Bash` (ledger reads are CLI calls) but the body never instructs a `--live` call without `--budget`.
- [ ] Frontmatter valid; body < 500 lines.
**Test plan** — `test_plugin_skills.py`: frontmatter parse; assert determinism + ledger-reading content; assert no example fires `--live` without `--budget`.
**Security review notes** — Teaching `retryable:false` = stop prevents the agent from retry-looping past a security rejection. The `--budget`-always rule supports the §12.4 cost-governance gate.

### CRA-251 — Plugin slash commands wrapping `craw code` verbs
**Milestone:** M3 · **Priority:** Medium · **Depends on:** CRA-245/246/247, UNFILED-MAP
**Context** — RFC §4: thin `/craw-*` wrappers give a human one-keystroke access to the
agent/dev-time verbs. CC platform fact: slash commands are *legacy*; the modern idiom is a
**skill with `disable-model-invocation:true`** (user-only, side-effecting command). We ship
both a `commands/` dir (compat) and prefer skill-form for the side-effecting ones.
**Design** — For each verb (`init`, `new`, `sync`, `map`, `describe`, `eval`) ship a thin
wrapper that shells out to `craw code … --json`. The **side-effecting** ones
(`init`, `new`) are skills with `disable-model-invocation:true` so the model can't fire them
autonomously (only the user can `/crawfish-new`); the **read-only** ones (`map`, `describe`,
`sync`) may stay model-invocable. Wrappers add no logic — they are the RFC's "thin wrappers
over `craw …`," upholding the one-execution-path rule.
**Interface** — `commands/craw-new.md` (legacy form) and the preferred skill form:
```yaml
---
name: crawfish-new
description: Author a new Crawfish component from a template (definition/pipeline/source/sink/tool/observer/policy/mcp).
disable-model-invocation: true       # user-only — it writes files
allowed-tools: Bash
---
Run `craw code new <kind> <name> --json` and report the created files.
```
```text
/crawfish-new definition triage     → craw code new definition triage --json
/crawfish-map                       → craw code map --json
/crawfish-sync                      → craw code sync --json
```
**Acceptance criteria**
- [ ] Every `craw code` verb has a `/crawfish-*` wrapper.
- [ ] Side-effecting wrappers (`init`,`new`) set `disable-model-invocation:true`; read-only ones do not.
- [ ] Wrappers contain no logic beyond the `craw code … --json` shell-out.
- [ ] Command names are `crawfish-*`-prefixed (no collision with export subagents).
**Test plan** — `packages/crawfish/tests/test_plugin_commands.py`: every verb has a wrapper; side-effecting ones are model-disabled; the wrapped command string matches the verb; name prefix check.
**Security review notes** — `disable-model-invocation:true` on `new`/`init` is the safety control: an injected model can't autonomously scaffold/author a capability without a human keystroke. Read-only wrappers (`map`/`describe`) route through the same redaction as their verbs (CRA-271).

### UNFILED-PIN — Pin, version-range, and integrity-check the plugin bundle
**Milestone:** M3 · **Priority:** Medium · **Depends on:** CRA-245, `crawfish.lock`/`craw freeze`/`craw doctor`
**Context** — The plugin is the *source of the security rules*; an unpinned bundle can be
silently swapped (§12.2). CC has **no `claude.lock`** (manifest semver only), so the
**framework** must pin the bundle in `crawfish.lock` — a pin distinct from CC's own dep
resolution.
**Design** — `craw freeze` (and `craw code init`) compute a `bundle_sha256` over the
plugin tree (deterministic, sorted-file digest, mirroring the Definition content-sha
exclusions) and record it in `crawfish.lock` alongside the `requires_crawfish` range from
`plugin.json`. `craw doctor` re-verifies the on-disk bundle against the pinned digest and
flags a mismatch (fail-closed). `craw code sync` runs the same compat check: a plugin whose
`requires_crawfish` range excludes the installed `crawfish` version fails closed (§12.3 the
plugin-not-lockstepped gap). Optional publisher signature is a later add (recorded as
deferred).
**Interface**
```jsonc
// crawfish.lock fragment
{ "plugin": { "name": "crawfish", "version": "0.2.0",
              "bundle_sha256": "sha256:…", "requires_crawfish": ">=0.2,<0.3" } }
```
```text
craw doctor                 # flags: plugin bundle digest mismatch / version skew
craw code sync              # exit 1 + craw.error.v1 code="plugin_skew" on incompatible range
```
**Acceptance criteria**
- [ ] `craw freeze`/`init` write `bundle_sha256` + `requires_crawfish` to `crawfish.lock`.
- [ ] `craw doctor` flags a tampered bundle (digest mismatch), fail-closed.
- [ ] `craw code sync` fails closed on a `requires_crawfish` range that excludes the installed version.
- [ ] The digest computation is deterministic (sorted files, stable exclusions).
**Test plan** — `packages/crawfish/tests/test_plugin_pin.py`: freeze writes pin; tamper a skill file → doctor flags mismatch; incompatible range → sync `plugin_skew`; digest stability across two runs.
**Security review notes** — Rule 6 (supply chain). This closes "the plugin's own bundle is unpinned" (§12.2). The digest is the integrity anchor; the range check prevents a stale plugin teaching rules that no longer match the framework's enforcement.

> **Spec correction (M3 build, UNFILED-PIN).** As shipped:
> - **Pin file.** The plugin pin lives in its own JSON file, **`crawfish.plugin.lock`** (top-level `{ "plugin": { name, version, bundle_sha256, requires_crawfish } }`), *not* in `crawfish.lock`. `crawfish.lock` is already overloaded — `crawfish.build` consumes it as a **pip requirements** file (`pip install --requirement crawfish.lock`), and the resolve-closure lock is the distinct `crawfish.closure.lock`. Writing a JSON document into the pip-requirements file would corrupt the build. The pinned *fields* and fail-closed semantics are exactly as specified; only the filename differs. The helper is `crawfish.code.plugin` (`compute_pin`/`write_pin`/`read_pin`/`verify_bundle`/`requires_satisfied_by`).
> - **Version alignment.** The framework is at **0.3.0**, so the shipped `plugin.json` pins `version: "0.3.0"` and `requires_crawfish: ">=0.3,<0.4"` (the spec's `0.2.0` / `>=0.2,<0.3` example predated the 0.3.0 release).
> - **`plugin_skew` exit code.** `craw code sync` surfaces an incompatible range as `craw.error.v1` `code="plugin_skew"`, **exit 1** (`expected_failure`, `retryable:true`) — a recoverable compat finding, *not* a security rejection (it is not in `SECURITY_CODES`). A *tampered* bundle (digest mismatch) is surfaced by `craw doctor` as an `error`, fail-closed.
> - **Author URL.** `plugin.json` uses the repo's real author URL (`github.com/Neal-Kotval/crawfish`).

---

## M3a — Authoring playbook (definition contents)

M3a is the file-by-file authoring spec, shipped **both** as plugin skills (under
`crawfish-authoring/`, progressive-disclosure siblings) **and** as the source-of-truth doc
the validation eval (CRA-265) checks. The router skill `crawfish-authoring/SKILL.md` is
`user-invocable:false` (background knowledge) and indexes the siblings.

### CRA-256 — Write the definition authoring spec (source of truth)
**Milestone:** M3a · **Priority:** Urgent · **Depends on:** `docs/reference/definition.md`
**Context** — The authoring knowledge must have one canonical home that both the plugin
skills and the validation eval derive from, so teaching and checking never drift.
**Design** — A markdown spec (this section's siblings, rooted at
`docs/specs/craw-code/authoring/` and mirrored into `crawfish-authoring/`) that walks the
Definition directory contract (`definition.md`) file-by-file, each section the source for
one skill (CRA-258..264) and one set of validation assertions (CRA-265). It states the
static/fluid spine inline at every file that can touch it.
**Interface** — `docs/specs/craw-code/authoring/README.md` index → one section per file.
The plugin build (`craw code build-plugin`) copies each section into
`crawfish-authoring/<file>.md`.
**Acceptance criteria**
- [ ] One canonical section per Definition file kind, each tagged with the skill + eval it feeds.
- [ ] Every section that can touch a sink/secret/fluid value states the spine inline.
- [ ] The plugin skills are *derived from* (not duplicated from) this source.
**Test plan** — `packages/crawfish/tests/test_authoring_spec.py`: assert each authoring skill body is byte-derived from its source section (no drift); assert every file kind from `definition.md` has a section.
**Security review notes** — Single-source-of-truth prevents the teaching surface drifting from the enforcement; the spine appears at every relevant file so the agent can't author a file in isolation without the rule.

### CRA-257 — Build a golden worked-example definition
**Milestone:** M3a · **Priority:** Urgent · **Depends on:** CRA-256, CRA-258..264
**Context** — A complete, idiomatic Definition the agent can pattern-match against and the
eval can load. The `demo/triage-bot/` dogfood project is the home (CLAUDE.md: "extended and
run every milestone").
**Design** — Flesh `demo/triage-bot/` into the canonical golden example exercising every
file kind: `definition.py` (typed IO, static project + fluid ticket), `instructions.md` +
`agents/*.md` (lead + classifier + summarizer, matching `crawfish.scaffold.FILES`),
`tools/*.py` (a callable whose name = filename stem, with taint-aware IO),
`mcp/*.py` (an `MCPConnection`, `auth` by reference), `policies/*.py` (a module-level
`Policy`), `skills/*.md`, knowledge (`with_context` over a `Wiki`), and `fixtures/`. It must
`load_definition` clean and pass `craw test`.
**Interface** — `demo/triage-bot/` complete tree (extends the existing scaffold seed).
**Acceptance criteria**
- [ ] The example exercises every Definition file kind from `definition.md`.
- [ ] `load_definition("demo/triage-bot")` compiles clean (no `DefinitionLoadError`).
- [ ] It marks at least one `Flow.STATIC` and one fluid `Parameter`; `mcp` auth is by reference.
- [ ] `craw test demo/triage-bot --fixtures fixtures` passes under `MockRuntime`.
- [ ] It is the example the CRA-265 validation eval loads.
**Test plan** — `packages/crawfish/tests/test_golden_definition.py`: `load_definition` clean; every asset kind present in `defn.assets`; flow tags; mock `craw test` green.
**Security review notes** — The golden example is what the agent imitates, so it must be exemplary: reference-only auth, static sink target, fluid ticket body. A red-team variant (fluid→sink) is kept under fixtures to prove the assembly gate rejects it (CRA-265).

### CRA-258 — `definition.py` (typed IO + team shape)
**Milestone:** M3a · **Priority:** High · **Depends on:** CRA-256
**Context** — `definition.py` declares typed inputs/outputs (`Parameter`, static/fluid) and
the team shape (`lead`/`coordination`) — the spine's primary surface (`definition.md`).
**Design** — Skill `crawfish-authoring/definition-py.md`: how to declare `inputs`/`outputs`
as `Parameter`s with `Flow.STATIC` (author config: project, sink target) vs default-fluid
(untrusted data: a ticket body), set `lead`/`coordination`, and never derive a consequential
setting from a fluid value (`with_*` never widens fluidity — `variables-and-knowledge.md`).
**Interface**
```yaml
---
name: crawfish-authoring-definition-py
description: Author definition.py — typed inputs/outputs (Parameter, static vs fluid) and the team shape (lead/coordination).
user-invocable: false
allowed-tools: Read, Grep
---
```
Body: the `Parameter`/`Flow` model; static = set-once consequential config, fluid = untrusted
per-item data; `lead`/`coordination` inference; the "never derive a static slot from fluid"
rule. Worked snippet = the golden `definition.py`.
**Acceptance criteria**
- [ ] Skill body shows a `definition.py` with both a static and a fluid `Parameter`.
- [ ] States the no-fluid-into-consequential-setting rule.
- [ ] Derived from CRA-256 source; `user-invocable:false`.
**Test plan** — `test_authoring_spec.py`: snippet `load_definition`s clean; flow tags asserted; derivation check.
**Security review notes** — Rule 1. This is where the agent first encounters the boundary; the skill must make fluid the *default* and static the *deliberate, consequential* choice.

### CRA-259 — `instructions.md` & `agents/*.md`
**Milestone:** M3a · **Priority:** High · **Depends on:** CRA-256
**Context** — `instructions.md` is the lead's prompt; `agents/*.md` are subagents, each
front-matter (`role`, `delegates_to`, `tools`, `model`) over a markdown body (`definition.md`,
`claude-code-export.md` round-trip).
**Design** — Skill `instructions-agents.md`: front-matter keys, the `delegates_to` graph
(every role must be a real team role — load-time validated), the **fluid-as-data** rule for
prompts (a fluid input reaches the prompt as data, never concatenated as instructions —
SECURITY.md rule 1). Worked = the golden lead + classifier + summarizer.
**Interface**
```yaml
---
name: crawfish-authoring-instructions-agents
description: Author instructions.md and agents/*.md — front-matter (role, delegates_to, tools, model) over a markdown prompt; fluid inputs are data, never instructions.
user-invocable: false
allowed-tools: Read, Grep
---
```
**Acceptance criteria**
- [ ] Body covers front-matter keys + `delegates_to` validity (real roles only).
- [ ] States the fluid-input-as-data prompt rule.
- [ ] Worked example matches the golden team.
**Test plan** — `test_authoring_spec.py`: a sample team with a bad `delegates_to` raises `DefinitionLoadError`; a valid one loads.
**Security review notes** — Rule 1. The skill must forbid templating a fluid value into the instruction body; the compiler/runtime boundary (`runtime/prompt.py`) enforces it, the skill teaches it.

### CRA-260 — `tools/*.py` (callables + taint)
**Milestone:** M3a · **Priority:** High · **Depends on:** CRA-256
**Context** — Each `tools/*.py` defines a callable whose **name matches the filename stem**
(`definition.md`); host-side tool code runs out-of-process at run time with **taint
propagation** from fluid inputs (SECURITY.md rule 5).
**Design** — Skill `tools-py.md`: the filename-stem = callable-name contract, typed
signatures, and taint discipline (a value derived from a fluid input stays tainted and can
never silently become a static sink target or idempotency key). Note authoring-time-trusted
import (the compiler imports `tools/*.py`) vs run-time out-of-process execution — and that
under `craw code` the *author* may be the agent, so the M0 jailed compile applies.
**Interface**
```yaml
---
name: crawfish-authoring-tools-py
description: Author tools/*.py — a callable whose name matches the filename stem; typed IO with taint propagation from fluid inputs.
user-invocable: false
allowed-tools: Read, Grep
---
```
**Acceptance criteria**
- [ ] Body states the filename-stem = callable contract and the taint-propagation rule.
- [ ] Notes out-of-process run-time execution + M0 jailed compile for agent-authored tools.
- [ ] Worked example = golden `tools/*.py`.
**Test plan** — `test_authoring_spec.py`: a tool whose callable name ≠ stem raises `DefinitionLoadError`; a matching one is discovered into `assets`.
**Security review notes** — Rule 5 + §12.1 (agent-authored code = the trust collapse). The skill must point at the M0 jail: agent-authored `tools/*.py` is compiled jailed, not in-process-trusted.

### CRA-261 — `mcp/*.py` (connections, auth by reference)
**Milestone:** M3a · **Priority:** High · **Depends on:** CRA-256, CRA-277
**Context** — `mcp/*.py` holds module-level `MCPConnection` instances; `auth` is **always a
secret reference** (env-var name), never an inline credential (`definition.md` warning,
SECURITY.md rule 4). A new MCP re-enters the consent gate (CRA-277).
**Design** — Skill `mcp-py.md`: the `MCPConnection` fields (`command`/`url`,
`auth="<ENV_VAR>"`, `tools` allowlist), the reference-only rule, and that adding an MCP
triggers `craw code grant` (consent re-gate). Worked = golden `mcp/*.py`.
**Interface**
```yaml
---
name: crawfish-authoring-mcp-py
description: Author mcp/*.py — MCPConnection instances; auth is a secret reference (env-var name), never inline; a new connection re-enters the consent gate.
user-invocable: false
allowed-tools: Read, Grep
---
```
**Acceptance criteria**
- [ ] Body states `auth` is a reference (env-var name) only, never a value.
- [ ] Body states a new MCP triggers `craw code grant` re-consent (CRA-277).
- [ ] Worked example uses `auth="<ENV_VAR>"` + a `tools` allowlist.
**Test plan** — `test_authoring_spec.py`: golden `mcp/*.py` loads; an inline-secret variant trips the CRA-276 lint.
**Security review notes** — Rule 4 + CRA-277 consent re-gate. The skill teaches reference-only; the lint (CRA-276) and re-gate (CRA-277) enforce.

### CRA-262 — `policies/*.py` + `skills/*.md`
**Milestone:** M3a · **Priority:** Medium · **Depends on:** CRA-256
**Context** — `policies/*.py` holds module-level `Policy` instances (consequential, static
config — bound per-agent, load-time validated, `definition.md`); `skills/*.md` are bundled
skills.
**Design** — Skill `policies-skills.md`: how to declare a `Policy` (static consequential
config — `with_policy` adds a static policy, `variables-and-knowledge.md`), bind it on an
agent, and author a `skills/*.md` entry. States that a policy is consequential and therefore
static-only.
**Interface**
```yaml
---
name: crawfish-authoring-policies-skills
description: Author policies/*.py (module-level Policy instances, static consequential config) and skills/*.md (bundled skills).
user-invocable: false
allowed-tools: Read, Grep
---
```
**Acceptance criteria**
- [ ] Body shows a module-level `Policy` and an agent binding it.
- [ ] States policies are consequential/static-only.
- [ ] Covers `skills/*.md` discovery into `assets.skills`.
**Test plan** — `test_authoring_spec.py`: golden `policies/*.py` discovered; an agent binding an unknown policy raises `DefinitionLoadError`.
**Security review notes** — Rule 2 (consequential config is static). A `Policy` derived from a fluid value would violate the spine; the skill forbids it.

### CRA-263 — knowledge (Wiki / `with_context`)
**Milestone:** M3a · **Priority:** Medium · **Depends on:** CRA-256
**Context** — Knowledge attaches via `with_context`/`Wiki`/`SkillRef` and is **summoned as
tainted data** (`consult()` entries are tainted, `variables-and-knowledge.md`; SECURITY.md
language-era "retrieved knowledge is tainted").
**Design** — Skill `knowledge.md`: build a `Wiki` (`with_page`, `TrustTier`), attach by
pinned snapshot (`with_context(base, wiki, mode=SummonMode.READONLY)` — the body never enters
the reference/checksum), and the rule that summoned knowledge is data, never instructions,
and can never reach a static-only sink. Note a summoned wiki is frozen in eval mode.
**Interface**
```yaml
---
name: crawfish-authoring-knowledge
description: Attach knowledge with Wiki/with_context/SkillRef — summoned content is tainted data, never instructions, and is pinned by content hash.
user-invocable: false
allowed-tools: Read, Grep
---
```
**Acceptance criteria**
- [ ] Body shows `Wiki.with_page` + `with_context(..., mode=SummonMode.READONLY)`.
- [ ] States summoned knowledge is tainted (data) and the body never enters the export checksum.
- [ ] States a summoned wiki is frozen in eval mode.
**Test plan** — `test_authoring_spec.py`: attaching knowledge yields tainted `consult()` entries; the export checksum tracks the pin, not the body (mirrors `variables-and-knowledge.md`).
**Security review notes** — Language-era "retrieved knowledge is tainted" + rule 9 (aggregate taint is union). The skill must make clear taint can only be dropped via audited `declassify`, unreachable from a fluid path.

### CRA-264 — fixtures & evals
**Milestone:** M3a · **Priority:** Medium · **Depends on:** CRA-256, CRA-257
**Context** — `fixtures/` feeds `craw test` (eval-as-test, `docs/guide/cli.md`); evals gate
on a baseline (`craw eval --baseline`). The agent must author deterministic fixtures and
read the gate.
**Design** — Skill `fixtures-evals.md`: fixture JSON shape (`{"inputs": {…}}`, matching
`crawfish.scaffold` `fixtures/login-bug.json`), running `craw test` on the mock, and the
`craw eval` baseline gate (per-metric deltas, tolerance, the cost band). Determinism: all
randomness in `--seed`, mock-by-default.
**Interface**
```yaml
---
name: crawfish-authoring-fixtures-evals
description: Author fixtures/ for craw test and gate on a baseline with craw eval — deterministic, mock-by-default, randomness carried in --seed.
user-invocable: false
allowed-tools: Read, Grep, Bash
---
```
**Acceptance criteria**
- [ ] Body shows the fixture `{"inputs": {…}}` shape and a `craw test` invocation.
- [ ] Covers `craw eval --baseline`/`--tolerance` and reading per-metric deltas + the cost band.
- [ ] Stresses mock-by-default + `--seed` determinism.
**Test plan** — `test_authoring_spec.py`: golden fixture runs green under `craw test`/mock; eval against a saved baseline is deterministic.
**Security review notes** — Rule 7 (sinks fire only in eval mode) + determinism. Fixtures never carry secrets; the skill points at `.env.example` references-only.

### CRA-265 — Validation eval: authored definitions load clean
**Milestone:** M3a · **Priority:** Urgent · **Depends on:** CRA-256..264, CRA-257
**Context** — The playbook's value is that an agent following it produces a Definition that
`load_definition`s clean and passes the assembly gate. This eval is the regression that
proves the skills + golden example actually compose.
**Design** — A deterministic eval that, for the golden example and a small corpus of
playbook-derived fixtures, asserts: `load_definition` clean (no `DefinitionLoadError`), the
assembly gate passes (no fluid→static-sink), the secret-shaped lint is clean, and `craw test`
on the mock is green — and that a **negative** corpus (a fluid→sink wiring, an inline secret,
an unknown tool binding) is *rejected* with the right error. No live calls.
**Interface**
```text
craw code validate-authoring [--corpus tests/fixtures/authoring] [--json]
# exit 0 all clean+rejections-correct · 1 a positive failed to load · 7 assembly gate unexpectedly passed/failed
```
```jsonc
// craw.code.validate.v1
{ "schema": "craw.code.validate.v1",
  "positives": [ { "id": "triage-bot", "loads": true, "assembly_gate": "pass", "lint": "clean", "test": "green" } ],
  "negatives": [ { "id": "fluid-to-sink", "rejected_by": "assembly_gate", "code": "TargetMustBeStaticError" },
                 { "id": "inline-secret", "rejected_by": "secret_shaped_lint" } ],
  "verdict": "pass" }
```
**Acceptance criteria**
- [ ] Every positive fixture (and the golden example) `load_definition`s clean, passes the assembly gate + lint, and `craw test`s green on the mock.
- [ ] Every negative fixture is rejected by the expected gate with the expected error code.
- [ ] The eval is fully deterministic (no live model call).
**Test plan** — `packages/crawfish/tests/test_authoring_validation.py`: drive the positive + negative corpora; assert per-fixture verdicts; snapshot `craw.code.validate.v1`.
**Security review notes** — This is the behavioural proof that the playbook teaches the *enforced* shape: the negative corpus includes the red-team payloads (fluid→sink, inline secret) the assembly gate + lint must reject, satisfying SECURITY.md's "a change that adds a fluid surface must add an injection payload."

### UNFILED-OPT — Authoring skill "optimizing a component" (train/eval mode, baselines)
**Milestone:** M3a · **Priority:** Medium · **Depends on:** CRA-256, `craw eval`/`tune`/`refine`/`learn`
**Context** — RFC §12.4: the optimize half needs an authoring skill teaching how to improve
a component — train vs eval mode, baselines, the cost-regularized objective. Pairs with the
M4.5 `craw code optimize` orchestrator (specced elsewhere).
**Design** — Skill `optimizing-a-component.md`: the train/eval distinction (a frozen
eval-mode Definition gates; train mode searches), seeding a baseline (`craw eval
--set-baseline`), the knob search (`craw tune --models --max-trials --cost-per-trial
--budget --cost-regularized`), the refine loop (`craw refine --until score>=0.95`), and one
self-versioning cycle / rollback (`craw learn`). Determinism: same `--seed` → byte-identical
`winner` sha.
**Interface**
```yaml
---
name: crawfish-authoring-optimizing
description: Optimize a Crawfish component — set a baseline (craw eval), search knobs (craw tune), iterate (craw refine), self-version (craw learn); train vs eval mode, budget-bounded.
user-invocable: false
allowed-tools: Read, Grep, Bash
---
```
**Acceptance criteria**
- [ ] Body teaches train-vs-eval mode and the baseline → tune → refine → learn arc.
- [ ] Every example is budget-bounded (`--budget`) and seeded (`--seed`).
- [ ] States sinks never fire during optimize (eval-only).
**Test plan** — `test_plugin_skills.py`: frontmatter + content checks; assert no example runs unbounded `--live`.
**Security review notes** — Rule 7 (optimize never fires a sink) + cost governance (§12.4). The skill must forbid an unbudgeted `--live` search.

### UNFILED-O4 — ADR: export relationship (adopt subsumes export; disjoint `.claude/` namespaces)
**Milestone:** M3a · **Priority:** Medium · **Depends on:** UNFILED-ADOPT, `claude-code-export.md`
**Context** — RFC O-4 (resolved): `craw code adopt` subsumes `craw export --claude-code` as
its plugin-install step; the plugin owns `.claude/` *plugin* assets under a reserved
`crawfish-*` prefix, export owns per-Definition subagent files — disjoint namespaces,
preserving the `.claude`-excluded-from-`sha` invariant. This must be recorded as an ADR.
**Design** — An ADR in `docs/architecture/decisions/` stating: (1) the decision (complement,
not collision); (2) the namespace split — `.claude/plugins/crawfish/` (plugin) vs
`.claude/agents/<defn>.md` + `.claude/skills/<defn>/SKILL.md` (export), both under the
`.claude` sha-exclusion; (3) the reserved `crawfish-*`/`craw-*` component-name prefix; (4)
rejected alternatives (fork; single merged namespace; export-only). Cite the exclusion list
(`definition.md`) and the export mapping (`claude-code-export.md`).
**Interface** — `docs/architecture/decisions/0009-craw-code-export-relationship.md` (next ADR number — **verify number**), standard ADR template (Context / Decision / Consequences / Rejected alternatives).
**Acceptance criteria**
- [ ] ADR records the decision, the namespace split, the reserved prefix, and rejected alternatives.
- [ ] Cites the `.claude` sha-exclusion invariant and the export mapping.
- [ ] Referenced from UNFILED-ADOPT and the RFC O-4 resolution.
**Test plan** — `packages/crawfish/tests/test_claude_namespace.py`: after `adopt`, assert `.claude/plugins/crawfish/` and `.claude/agents/*.md` coexist without path collision; assert both are excluded from the Definition `sha`; assert all plugin component names carry the `crawfish-*` prefix.
**Security review notes** — Namespace disjointness preserves content identity (a perturbed sha would invalidate signatures/provenance). The reserved prefix prevents an agent-authored subagent from shadowing a plugin skill that teaches the security rules.

---

## Cross-cutting Definition of Done (every issue)

Per CLAUDE.md and RFC §11: `ruff` + `mypy` (strict) clean; `pytest` green and
deterministic (no live model calls — `MockRuntime`/record-replay); the security spine
upheld (fluid never reaches a sink target via the agent loop; secrets by reference;
agent-added capabilities re-gated); `demo/triage-bot/` exercises the change end to end; and
docs updated. No verb imports a concrete backend — `Store`/`ArtifactStore`/`AgentRuntime`
protocols only (a grep test under `code/` asserts this).
