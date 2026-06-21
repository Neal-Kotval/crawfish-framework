# Crawfish

[![CI](https://github.com/Neal-Kotval/crawfish/actions/workflows/ci.yml/badge.svg)](https://github.com/Neal-Kotval/crawfish/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/crawfish.svg)](https://pypi.org/project/crawfish/)
[![Python](https://img.shields.io/pypi/pyversions/crawfish.svg)](https://pypi.org/project/crawfish/)
[![License: Apache-2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Docs](https://img.shields.io/badge/docs-mkdocs-blue.svg)](https://neal-kotval.github.io/crawfish/)

**Build agents like software.** Crawfish is a framework for defining an agent — or a whole
team of them — as typed, versioned components in a directory, running it locally against
`claude -p` or a local model, and treating the result as something you can test, diff, and
improve, not a prompt you keep poking at.

It brings the things you expect from real engineering to agent work:

- **Agents as code.** The agents, their tools, the data shapes they pass, and the policies
  that govern them are plain files you check into git — infrastructure-as-code for local
  model work, not settings buried in a notebook.
- **Composable by design.** Small typed nodes snap together into larger pipelines. One
  node's output wires to the next only when their shapes match, so a team is assembled from
  parts the way a program is.
- **Deterministic and testable.** Typed inputs and outputs, structural type-checking, frozen
  versions, and record/replay make a run reproducible. Snapshot it, assert on it, and gate
  changes in CI — no live model required.
- **Built to improve.** Score a pipeline with rubrics and evals, then let the tuner search
  for better prompts and settings and promote the winner. Pipelines get better on purpose.
- **Local-first.** Everything runs on your machine by default. Cloud and scale are a driver
  swap, not a rewrite.

Running a job over your data in bulk is one thing you can build this way — fan it out across
thousands of items, reduce, branch, and write the results somewhere
(`Source → Batch → Aggregator → Router → Sink`). The same building blocks just as easily
express a single sharp agent, a multi-agent team, or a scheduled automation.

## Install

Pick the line that matches what you're doing:

| You want to… | Install with | Why |
| --- | --- | --- |
| **Build *with* the framework** (`import crawfish`) | `pip install crawfish` &nbsp;·&nbsp; `uv add crawfish` | Lands in your project env so it resolves against your deps |
| **Just run the `craw` CLI** | `uv tool install crawfish` &nbsp;·&nbsp; `pipx install crawfish` | Isolated CLI, no env to pollute |
| **Try it with zero Python setup** | `curl -LsSf https://raw.githubusercontent.com/Neal-Kotval/crawfish/main/install.sh \| sh` | Bootstraps `uv` if needed, then installs the CLI |

The `curl` line is a thin wrapper over the same PyPI package — see [`install.sh`](install.sh).

Then run the zero-key demo:

```bash
craw init my-app
craw dev my-app/definitions/triage-bot -i project=acme -i "ticket_body=login is broken"
```

A team of agents runs on a mock runtime — no API key, no cost — and the result comes back
typed. Swap in `claude -p` for a real run; it's a runtime change, not a code change.

## Develop from source

This repo is a [`uv`](https://docs.astral.sh/uv/) workspace and uses
[`just`](https://github.com/casey/just) as its task runner — run `just` to see every recipe.

```bash
just deps              # install the workspace + dev deps (uv sync)
just demo              # run the demo end to end (zero key, mock runtime)
just check             # lint + typecheck + the full test suite
```

Or drive the CLI directly with `uv run craw …`. See
[`CONTRIBUTING.md`](.github/CONTRIBUTING.md) to go from a clone to a merged PR — the most
welcome first contribution is a connector.

## Docs

📖 **Full documentation: [neal-kotval.github.io/crawfish](https://neal-kotval.github.io/crawfish/)**

- [Getting started](https://neal-kotval.github.io/crawfish/guide/getting-started/) — install and run your first agent in minutes
- [Reference](https://neal-kotval.github.io/crawfish/reference/) — every public symbol, explained, with runnable examples
- [Architecture](docs/architecture/ARCHITECTURE.md) — the three swappable seams · [ADRs](docs/architecture/decisions)
- [Security](docs/architecture/SECURITY.md) — the prompt-injection boundary, secrets, and taint
- [Roadmap](ROADMAP.md) — what shipped and what's next

Browse the docs locally with `just docs` (serves at http://127.0.0.1:8000).

## Contributors

Thanks to everyone who has helped build Crawfish. 🦞

[![Contributors](https://contrib.rocks/image?repo=Neal-Kotval/crawfish)](https://github.com/Neal-Kotval/crawfish/graphs/contributors)

Get started as a contributor!
[`CONTRIBUTING.md`](.github/CONTRIBUTING.md).

## Star History

[![Star History Chart](https://api.star-history.com/svg?repos=Neal-Kotval/crawfish&type=Date)](https://star-history.com/#Neal-Kotval/crawfish&Date)

## License

[Apache-2.0](LICENSE).
