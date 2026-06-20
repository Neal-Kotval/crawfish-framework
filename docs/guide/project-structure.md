# Project structure & `craw doctor`

A Crawfish project is a directory of authored components plus generated state. By default,
discovery follows the standard layout. When a different shape fits your repo, you can move
folders via `crawfish.toml`. `craw doctor` checks the structure and flags problems.

## The canonical layout

```text
my-app/
├── crawfish.toml          # project config + profiles
├── sources/               # Source nodes (pipeline ingress)
├── sinks/                 # Sink nodes (the egress side effect)
├── definitions/           # Definition directories (the agent teams)
├── pipelines/             # Workflows wiring source → batch → … → sink
├── observers/             # Observer definitions
├── tools/                 # tool callables
├── policies/              # Policy instances
└── .crawfish/             # GENERATED — locks, ledger, cassettes, registry (gitignore)
```

You write and commit the first seven folders. `.crawfish/` is generated: locks, the
execution ledger, cassettes, and the deploy registry. Crawfish keeps the two separate so
your intent and the machine's state never mix.

## Relocating folders — `[project.paths]`

Override any folder location in `crawfish.toml`. The CLI, the compiler, and `craw doctor`
all read the configured path, so discovery follows the override everywhere:

```toml
[project.paths]
definitions = "agents/"      # author Definitions under agents/ instead
observers   = "watch/"
# unset keys keep their canonical default (sources/, sinks/, …)
```

After this, `craw deploy agents/triage-bot` and `Definition.from_package("agents/...")`
resolve against the relocated tree; nothing else changes.

## `craw doctor`

```bash
craw doctor
```

`craw doctor` checks three things. It confirms each configured folder exists. It flags
misplaced files, such as a Definition sitting in `tools/` or a stray Python file outside
any known folder. And it verifies that authored and generated state stay separate: nothing
under `.crawfish/` is hand-edited, and no generated artifact has leaked into the authored
tree.

## Worked example

Run it against the demo project:

```bash
craw doctor
# project: my-app   (crawfish.toml ok)
# ✔ sources/        2 files
# ✔ definitions/    1 (triage-bot)
# ✔ pipelines/      1
# ✔ observers/      1 (quality)
# ✔ .crawfish/      generated — clean (ledger, registry, cassettes)
# ⚠ tools/format.py  looks like a Definition (has instructions.md) — move to definitions/?
# doctor: 1 warning
```

To fix the warning, move the misplaced directory. If you meant to use a custom layout,
declare it in `[project.paths]` instead and re-run. The warning clears once discovery and
the filesystem agree.

## Security

`craw doctor` reads the filesystem and `crawfish.toml` only. It never resolves a secret or
runs a model. The authored-vs-generated check is itself a guardrail. Keeping `.crawfish/`
(the ledger, registry, cassettes) separate from authored components means a generated
artifact can't be mistaken for trusted authored config. That's the same boundary that
keeps [deploy](deploy.md) and [observers](observers.md) honest. See
[SECURITY.md](../architecture/SECURITY.md).
