# Writing craw code docs

`craw code` is a large surface — a CLI verb family, a plugin, a dashboard, and an authoring
playbook — built by many hands (and many agents). This page codifies the house style so the
docs stay coherent no matter who writes them. If you are adding or changing a `craw code`
page, read this first.

!!! note "You will learn:"
    - The page skeleton every guide page follows
    - When to write a **guide** page versus a **reference** page
    - How to register a new page in `mkdocs.yml` so it actually appears
    - The conventions for runnable examples, security admonitions, and determinism

## The voice

Crawfish docs read as **warm, narrative prose** — you are explaining a system to a colleague,
not dumping a feature list. Lead with the *why*, then show the *how* with a runnable example,
then tabulate the options. Avoid marketing tone ("blazing-fast", "powerful"); state what the
thing does and what it protects you from. Compare `guide/getting-started.md` and
`guide/optimize-from-the-cli.md` — match that register.

## The page skeleton (guide pages)

Every guide page under `guide/craw-code/` follows the same shape:

1. **Title** (`#`) — the task, not the noun. "Author a project with craw code", not "Authoring".
2. **A one-paragraph hook** — what this page lets the reader do, and why it matters.
3. **A "You will learn:" admonition** near the top:

    ```markdown
    !!! note "You will learn:"
        - The first thing
        - The second thing
    ```

4. **Body** — prose interleaved with runnable fenced examples. Every page has at least one
   example a reader can run against the demo (`demo/triage-bot/` or `demo/craw-code-tour/`).
5. **Option/flag matrices as tables**, never as prose lists.
6. **Security boundaries as `!!! warning` admonitions** (see below).
7. **A "See also" tail** cross-linking the reference pages and adjacent guides.

## Guide vs reference

| Write a **guide** page when… | Write a **reference** page when… |
| --- | --- |
| You are teaching a task end to end (init a project, operate a run, approve a change) | You are documenting a typed surface exhaustively (the `craw.error.v1` schema, the provenance record) |
| The reader follows along and runs commands | The reader looks up a field, flag, or exit code |
| Narrative order matters | Completeness and stable anchors matter |

`craw code` guides live in `guide/craw-code/`; its reference pages live in `reference/`
(`reference/craw-code-provenance.md`, `reference/craw-code-json-contracts.md`). Do not
duplicate reference material into guides — link to it.

## Registering a page in the nav

A Markdown file that is not in `mkdocs.yml` `nav:` **will not appear in the site**. The
`craw code` guide group sits after **Operate**:

```yaml
  - craw code:
      - Overview & quickstart: guide/craw-code/index.md
      - Author a project with craw code: guide/craw-code/authoring.md
      - The craw code CLI: guide/craw-code/cli.md
      - The dashboard: guide/craw-code/dashboard.md
      - Operate & optimize: guide/craw-code/operate.md
      - Review & approve (HITL): guide/craw-code/review-and-approve.md
      - Security model: guide/craw-code/security.md
      - Writing craw code docs: guide/craw-code/writing-docs.md
```

Reference pages register under the existing **Reference** group.

## Security admonitions

`craw code` collapses a trust boundary — agent-authored code is no longer
authoring-time-trusted. Wherever a page touches that boundary, mark it explicitly so a
reader skimming cannot miss it:

```markdown
!!! warning "Trust boundary"
    Code authored by the agent is provenance-stamped and jailed at compile. A consequential
    sink target or idempotency key derived from `Flow.FLUID` data is rejected by construction
    — it is never merely discouraged.
```

Use `!!! warning` for trust/security boundaries, `!!! note` for "You will learn" and asides,
and `!!! danger` only for genuinely destructive operations (e.g. `--live` promotion).

## The determinism rule

Every example must be **runnable and deterministic**. Use the mock runtime / recorded
cassettes that ship with the demo — never a snippet that requires a live model call or
network. If an example shows `--live`, frame it as the gated, opt-in path and show the
preview/approval step first. This mirrors the test suite's rule: no live model calls.

## See also

- [The craw code CLI](cli.md) — the verb reference these guides teach against
- [Security model](security.md) — the trust-boundary collapse in depth
- `docs/dev/craw-code/00-ORCHESTRATION-LOG.md` (in the repo) — how the build was structured
