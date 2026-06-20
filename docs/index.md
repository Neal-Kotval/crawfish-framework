# Crawfish

Crawfish runs agents over your data in bulk: fan a job out across many items, reduce
the results, branch on them, and write them somewhere. You write each pipeline as a
folder of files and run it on your own machine with `claude -p` — no API key for the
dev loop. Runs are typed and versioned, so you can diff and replay them.

The shape of a pipeline: `Source → Batch → Aggregator → Router → Sink`. If you've used
dbt or Airflow, the idea is familiar — this is that, for agents.

## Start here

- **[Getting started](guide/getting-started.md)** — install and run your first agent in a few minutes
- **[Tutorial](guide/tutorial.md)** — build the triage bot end to end
- **[Concepts](guide/concepts.md)** — the directory model, pipelines, runtimes, and the security boundary
- **[Cookbook](guide/cookbook.md)** — copy-paste recipes
- **[API reference](guide/api-reference.md)** — the public surface

## Operate and observe

Run a pipeline once, or keep it running. These pages cover deploying, watching, and
controlling pipelines locally.

- **[Operations overview](guide/operations.md)** — the deploy → observe → visualize → manage loop
- **[Deploy](guide/deploy.md)** — `craw deploy`: a detached, scheduled, self-restarting supervisor
- **[Manage](guide/manage.md)** — `craw manage`: list, stop, restart, and tail logs for deployed pipelines
- **[Observers](guide/observers.md)** — `crawfish.observe`: rule- and LLM-based watchers over a run
- **[Visualize](guide/visualize.md)** — `craw visualize`: a localhost-only dashboard
- **[Project structure](guide/project-structure.md)** — the standard layout, `[project.paths]`, and `craw doctor`
- **[Export to Claude Code](guide/claude-code-export.md)** — `craw export --claude-code`: run a Definition as a subagent

## Under the hood

- **[Architecture](architecture/ARCHITECTURE.md)** — the three swappable seams · [ADRs](architecture/decisions)
- **[Security](architecture/SECURITY.md)** — the prompt-injection boundary, secrets, and taint
- **[API stability](architecture/API-STABILITY.md)** — semver and deprecation policy
- **[Product](product/PRODUCT.md)** — positioning, hero use case, personas
- **[Roadmap](roadmap/README.md)** — the Phase-1 plan

## The 30-second version

```bash
uv sync
craw init my-app && cd my-app
craw dev definitions/triage-bot -i project=acme -i "ticket_body=login is broken"
```
