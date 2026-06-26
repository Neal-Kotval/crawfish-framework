<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/crawfishai/crawfish/main/docs/assets/logo-dark.svg">
    <img src="https://raw.githubusercontent.com/crawfishai/crawfish/main/docs/assets/logo.svg" alt="Crawfish" width="420">
  </picture>
</p>

[![CI](https://github.com/crawfishai/crawfish/actions/workflows/ci.yml/badge.svg)](https://github.com/crawfishai/crawfish/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/crawfish.svg)](https://pypi.org/project/crawfish/)
[![Python](https://img.shields.io/pypi/pyversions/crawfish.svg)](https://pypi.org/project/crawfish/)
[![License: Apache-2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Docs](https://img.shields.io/badge/docs-mkdocs-blue.svg)](https://crawfishai.github.io/crawfish/)

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
| **Try it with zero Python setup** | `curl -LsSf https://raw.githubusercontent.com/crawfishai/crawfish/main/install.sh \| sh` | Bootstraps `uv` if needed, then installs the CLI |

The `curl` line is a thin wrapper over the same PyPI package — see [`install.sh`](install.sh).

Then run the zero-key demo:

```bash
craw init my-app
craw dev my-app/definitions/triage-bot -i project=acme -i "ticket_body=login is broken"
```

A team of agents runs on a mock runtime — no API key, no cost — and the result comes back
typed. Swap in `claude -p` for a real run; it's a runtime change, not a code change.

## A quick look

An agent is a **directory**, not a prompt string. The directory compiles to a typed
`Definition`: its inputs and outputs are declared as `Parameter`s, and a `Flow` tag marks
whether a value is trusted config or untrusted session data — the heart of the security model.

```text
definitions/triage-bot/
├── definition.py        # the typed IO boundary (below)
├── instructions.md      # the lead agent's brief
└── agents/
    ├── classifier.md     # a sub-agent (front-matter declares its role)
    └── summarizer.md
```

```python
# definitions/triage-bot/definition.py
from crawfish.core import Flow, Parameter

inputs = [
    Parameter(name="project", type="str", flow=Flow.STATIC),   # trusted config
    Parameter(name="ticket_body", type="str"),                  # default Flow.FLUID — untrusted
]

# The model's analysis is a fluid output — that's fine, it's data. The security rule bites
# elsewhere: a *consequential sink target or idempotency key* must be Flow.STATIC, so a fluid
# value can never steer where a write lands. The assembly gate (ALG-3) proves this at build
# time, and fluid input always reaches the model as data, never as instructions.
outputs = [Parameter(name="triage", type="str")]                # default Flow.FLUID

lead = "lead"
```

Load it in Python and you get a typed `Definition` you can inspect, freeze, diff, and
assert on — no live model needed:

```python
from crawfish import load_definition

defn = load_definition("definitions/triage-bot")   # compiles the directory to a typed Definition
for p in defn.inputs:
    print(p.name, p.type, p.flow.value)            # project str static · ticket_body str fluid
```

Run it from the CLI with `craw dev …` (above) on a mock runtime, or drive `run_team(...)`
from Python for full control. Because the IO is typed and versioned, you can `diff` two
versions, `replay` a past run for `$0`, score it against a golden set, and let the tuner
promote a better version — all from the CLI. See the
[tutorial](https://crawfishai.github.io/crawfish/guide/tutorial/).

## craw code — let an agent build and run it for you

[**craw code**](https://crawfishai.github.io/crawfish/guide/craw-code/) lets an LLM agent
(in Claude Code) **author and operate** a Crawfish project for you — and it's the same
trust model, *enforced*. When a model writes the code, that code is no longer trusted just
because it was authored: it's **provenance-stamped**, **jailed at compile** (agent-authored
code never executes in your shell), and **gated before it can go live** behind a fail-closed
human approval step. The CLI is the one execution path; a Claude Code plugin (authoring
skills + slash commands) is the ergonomics; a loopback-only, scrubbed dashboard is the read
surface.

It ships in the same package:

```bash
pip install crawfish
craw code init my-app                      # scaffold a project + ledger + Claude Code plugin

# …an agent in Claude Code now authors definitions using the craw code skills…

craw code describe my-app/definitions/triage-bot   # typed reflection via a jailed compile
craw code sync --dir my-app                 # assembly gate: fluid→static-sink rejected, lock regenerated
craw code estimate my-app/definitions/triage-bot --items 100   # cost preview, no model call
craw code dashboard --project my-app         # scrubbed, loopback ledger view
craw code propose … && craw code apply …     # nothing goes live without a recorded human approval
```

Every verb speaks `--json` with a stable schema and a small, closed set of exit codes, so an
agent can drive the whole loop over Bash and branch on the result. Read the
[craw code overview](https://crawfishai.github.io/crawfish/guide/craw-code/),
[security model](https://crawfishai.github.io/crawfish/guide/craw-code/security/), and
[CLI reference](https://crawfishai.github.io/crawfish/guide/craw-code/cli/).

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

📖 **Full documentation: [crawfishai.github.io/crawfish](https://crawfishai.github.io/crawfish/)**

- [Getting started](https://crawfishai.github.io/crawfish/guide/getting-started/) — install and run your first agent in minutes
- [Tutorial](https://crawfishai.github.io/crawfish/guide/tutorial/) — build the triage bot end to end
- [craw code](https://crawfishai.github.io/crawfish/guide/craw-code/) — let an agent author and operate a project, safely
- [Reference](https://crawfishai.github.io/crawfish/reference/) — every public symbol, explained, with runnable examples
- [Architecture](docs/architecture/ARCHITECTURE.md) — the three swappable seams · [ADRs](docs/architecture/decisions)
- [Security](docs/architecture/SECURITY.md) — the prompt-injection boundary, secrets, and taint
- [Roadmap](ROADMAP.md) — what shipped and what's next

Browse the docs locally with `just docs` (serves at http://127.0.0.1:8000).

## Contributors

Thanks to everyone who has helped build Crawfish. 🦞

[![Contributors](https://contrib.rocks/image?repo=crawfishai/crawfish)](https://github.com/crawfishai/crawfish/graphs/contributors)

Get started as a contributor!
[`CONTRIBUTING.md`](.github/CONTRIBUTING.md).

## Star History

[![Star History Chart](https://api.star-history.com/svg?repos=crawfishai/crawfish&type=Date)](https://star-history.com/#crawfishai/crawfish&Date)

## License

[Apache-2.0](LICENSE).
