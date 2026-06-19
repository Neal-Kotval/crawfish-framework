# crawfish-framework

**Agents for bulk work over your data** — `Source → Batch (fan-out) → Aggregator
(reduce) → Router (branch) → Sink`, authored as directories and run locally via
`claude -p` with zero API key. Measured + trustworthy: typed, versioned, benchmarked.

Think *dbt / Airflow for agents*, not another chatbot SDK.

## Quick start

This repo uses [`just`](https://github.com/casey/just) as its task runner — run `just`
to see every recipe.

```bash
just deps              # install the workspace + dev deps (uv sync)
just demo              # run the demo end to end (zero key, mock runtime)
just check             # lint + typecheck + the test suite
```

Or drive the CLI directly:

```bash
uv run craw init my-app                                  # scaffold a project
uv run craw dev my-app/definitions/triage-bot -i project=acme -i "ticket_body=login broken"
```

## Docs

- [Product](docs/product/PRODUCT.md) — positioning, hero use case, personas
- [Architecture](docs/architecture/ARCHITECTURE.md) — the three seams · [ADRs](docs/architecture/decisions)
- [Security spine](docs/architecture/SECURITY.md)
- [Roadmap](docs/roadmap/README.md) — the live Phase 1 plan (CRA-98, M0–M5)
- [Getting started](docs/guide/getting-started.md)

Status: **Phase 1 complete** — M0–M5 shipped (34/35 CRA-98 issues; Company Brain
deferred to the Phase-2 hub). The trust loop runs locally with no hosted dependency:
a multi-item Source fans out, a Definition team runs per item via `claude -p`, an
Aggregator reduces, a Router branches, and a Sink writes — typed, versioned,
benchmarked, with retries/dead-letter and crash-resume. `ruff` + `mypy --strict` clean;
255 tests green; docs build as a MkDocs site.

Browse the docs locally: `just docs` (serves at http://127.0.0.1:8000).

See [CLAUDE.md](CLAUDE.md) for development guidance and [docs/roadmap](docs/roadmap/README.md)
for the per-issue status.
