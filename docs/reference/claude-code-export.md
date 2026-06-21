# Claude Code export

Render a Crawfish [`Definition`](definition.md) — a self-contained agent team — into
Claude Code's own on-disk formats: a **subagent** (a Markdown file with YAML
front-matter) and, optionally, a **skill** (an invocable slash-command). A team authored
in Crawfish then runs as a native Claude Code teammate. These live in `crawfish.ccexport`.

**Symbols on this page:** `ClaudeCodeAgent` · `ClaudeCodeSkill` · `definition_to_cc_agent` ·
`export_claude_code` · `map_tools` · `model_alias`

---

## Core

A **Definition** is Crawfish's packaged agent team — the agents, their prompts, the
tools they may use, and the model each is pinned to. **Claude Code** (the CLI) has its
own way of describing an agent on disk:

- A **subagent** is a single Markdown file at `.claude/agents/<name>.md`. The top of the
  file is **YAML front-matter** — a small `key: value` header fenced by `---` lines —
  carrying the agent's `name`, `description`, `model`, and `tools`. Everything below the
  header is the **body**: the system prompt the agent runs with.
- A **skill** is a slash-command wrapper at `.claude/skills/<name>/SKILL.md`. Invoking it
  hands the task to the exported subagent.

This module is the translator. `definition_to_cc_agent` turns one Definition into a
`ClaudeCodeAgent` (the in-memory shape of that subagent file). `export_claude_code`
writes the file(s) to disk under a project's `.claude/` directory. Two helpers do the
field-level mapping: `map_tools` builds the tool allowlist, and `model_alias` picks the
Claude Code model name.

The **load-bearing rule**: the export carries **no secrets**. A Definition names its
credentials by reference — an MCP connection stores an environment-variable *name* (like
`GITHUB_TOKEN`), never the token itself. The export emits tool *names* only, never the
auth reference or any credential. The generated files are therefore safe to commit and
share.

> **MCP** (Model Context Protocol) is the standard by which an agent reaches an external
> tool server. A Crawfish Definition can declare MCP connections; each exposes a set of
> named tools the agent may call.

---

## Ramps up

### The subagent body is composed, not copied

`definition_to_cc_agent` does not paste one prompt. It composes the body from the whole
team, in a fixed order so the file is deterministic:

1. The **lead** (or, failing that, the agent whose role is `"main"`) goes first.
2. Remaining agents follow, sorted by role.
3. Each agent's prompt is appended.
4. Any **injected prompts** (extra system text attached to the Definition) come last,
   each under an `## injected: <target>` heading.

When the team has more than one agent, each block is titled with `## <role>`; a
single-agent team emits its prompt bare, with no heading. Empty prompts are skipped.

### The `tools` allowlist names tools, never secrets

`map_tools` produces the subagent's `tools` allowlist as the **union** of every agent's
declared tools and every MCP-exposed tool. An MCP tool is rendered in Claude Code's
qualified form `mcp__<server>__<tool>` — for a connection named `github` exposing
`search_issues`, the entry is `mcp__github__search_issues`. A bare MCP tool name listed
on an agent's own allowlist is dropped in favour of that qualified form (no duplicates).
The result is **sorted and de-duplicated** so the same Definition always yields the same
file. The MCP connection's `auth` reference is never emitted.

### Model mapping is Claude-first but universal

A Crawfish agent's `model` is **model-universal by default** — `None` means "let the
platform pick". `model_alias` maps whatever is pinned to one of Claude Code's three
aliases (`opus` / `sonnet` / `haiku`) by substring match, case-insensitively, so
`"claude-opus-4"` resolves to `opus` and `"haiku-3.5"` to `haiku`. A **list** of models
(a universal pin with preferences) resolves on its **first** entry. Anything
unrecognised — `"mock"`, an unknown id, or `None` — resolves to `inherit` (the platform
decides). It never raises, so an export always produces a runnable file. The
Claude-first runtime with a universal model type is [ADR
0005](../architecture/decisions/0005-claude-first-universal-model-type.md).

### Which agent's model wins

`definition_to_cc_agent` reads the model from the **lead** (or `"main"`) agent if that
agent has one pinned; otherwise it falls back to the first agent that pins any model;
otherwise `None` (which `model_alias` turns into `inherit`).

### The `description` is the lead's first prompt line

The front-matter `description` is the first non-empty line of the lead's (or `"main"`'s)
prompt, falling back to the first prompt line of any agent, and finally to
`"Crawfish definition <id>"`. The `name` is the Definition's `id` normalised to
kebab-case (Claude Code requires it).

### What `export_claude_code` writes

`export_claude_code` always writes `.claude/agents/<name>.md`, creating parent
directories as needed. With `skill=True` it **also** writes
`.claude/skills/<name>/SKILL.md` — a `ClaudeCodeSkill` whose body tells Claude Code to
invoke the exported subagent. It returns the list of written paths.

---

## API reference

### `ClaudeCodeAgent`

`class ClaudeCodeAgent(BaseModel)` — a Claude Code subagent: front-matter + body.

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `name` | `str` | — (required) | Kebab-case agent name (the file stem). |
| `description` | `str` | `""` | One-line front-matter description; omitted from output if empty. |
| `model` | `str` | `"inherit"` | A Claude Code alias (`opus`/`sonnet`/`haiku`) or `inherit`. |
| `tools` | `list[str]` | `[]` | The tool allowlist; omitted from output if empty. |
| `body` | `str` | `""` | The composed system prompt. |

`to_markdown() -> str` renders the `.claude/agents/<name>.md` file: a `---`-fenced
front-matter header (`name`, then `description` if set, then `model`, then `tools` as a
comma-joined list if non-empty) followed by the stripped body.

### `ClaudeCodeSkill`

`class ClaudeCodeSkill(BaseModel)` — a slash-command wrapper invoking the subagent.

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `name` | `str` | — (required) | Skill name (matches the agent name). |
| `description` | `str` | `""` | One-line front-matter description; omitted if empty. |
| `body` | `str` | `""` | Skill body — instructions to invoke the subagent. |

`to_markdown() -> str` renders `.claude/skills/<name>/SKILL.md`: a `---`-fenced header
(`name`, then `description` if set) followed by the stripped body.

### `definition_to_cc_agent`

```python
def definition_to_cc_agent(definition: Definition) -> ClaudeCodeAgent
```

Render a Definition into a `ClaudeCodeAgent`. Sets `name` from the kebab-cased
Definition `id`, `description` from the lead/`main` prompt's first line, `model` via
`model_alias` on the lead/`main` (or first-pinned) agent's model, `tools` via
`map_tools`, and `body` from the composed team prompt. Emits no secrets.

### `export_claude_code`

```python
def export_claude_code(
    definition: Definition,
    project_dir: Path,
    *,
    skill: bool = False,
) -> list[Path]
```

Write the subagent (and, with `skill=True`, the skill) under `project_dir/.claude`, and
return the written paths. Always writes `.claude/agents/<name>.md`; with `skill=True`
also writes `.claude/skills/<name>/SKILL.md`. Creates parent directories. Carries no
secrets.

### `map_tools`

```python
def map_tools(definition: Definition) -> list[str]
```

The subagent's `tools` allowlist: the sorted, de-duplicated union of every agent's
declared tools and every MCP-exposed tool. MCP tools render as `mcp__<server>__<tool>`;
a bare MCP tool name on an agent's allowlist is replaced by that qualified form. No
`auth` reference or credential is ever emitted.

### `model_alias`

```python
def model_alias(model: str | list[str] | None) -> str
```

Map a pinned model to a Claude Code alias. A list resolves on its first entry; the
string is matched case-insensitively against `opus` / `sonnet` / `haiku` by substring.
`mock`, an unrecognised id, or `None` resolves to `inherit`. Never raises. See
[ADR 0005](../architecture/decisions/0005-claude-first-universal-model-type.md).

| Input | Result |
| --- | --- |
| `"claude-opus-4"`, `"opus"` | `"opus"` |
| `"claude-sonnet-4"`, `"sonnet"` | `"sonnet"` |
| `"haiku-3.5"`, `"haiku"` | `"haiku"` |
| `["claude-opus-4", "sonnet"]` (list) | `"opus"` (first entry) |
| `"mock"`, unknown id, `None`, `[]` | `"inherit"` |

---

## Example

Build a small two-agent Definition with one MCP connection, convert it to a
`ClaudeCodeAgent`, and read the mapped fields — all in memory, nothing written to disk.

```python
from crawfish.definition.types import (
    Definition, TeamSpec, AgentSpec, Coordination, DefinitionAssets, MCPConnection,
)
from crawfish.ccexport import definition_to_cc_agent, map_tools, model_alias

team = TeamSpec(
    agents=[
        AgentSpec(
            role="main",
            prompt="Triage incoming issues and route them.\nUse the linked board.",
            model=["claude-opus-4", "sonnet"],   # universal pin, first entry wins
            tools=["Read", "Grep", "search_issues"],
        ),
        AgentSpec(role="fixer", prompt="Apply the fix.", tools=["Edit"]),
    ],
    coordination=Coordination.LEAD,
    lead="main",
)
assets = DefinitionAssets(
    # auth is a secret *reference* (an env-var name) — never emitted by the export
    mcp=[MCPConnection(name="github", auth="GITHUB_TOKEN",
                       tools=["search_issues", "create_pr"])]
)
d = Definition(id="Triage Bot", team=team, assets=assets)

agent = definition_to_cc_agent(d)
print("name:", agent.name)
print("description:", agent.description)
print("model:", agent.model)
print("tools:", agent.tools)
print("body:")
print(agent.body)
print("---")
print("map_tools:", map_tools(d))
print("model_alias(['claude-opus-4','sonnet']):", model_alias(["claude-opus-4", "sonnet"]))
print("model_alias('haiku-3.5'):", model_alias("haiku-3.5"))
print("model_alias('mock'):", model_alias("mock"))
print("model_alias(None):", model_alias(None))
```

??? success "▶ Output"

    ```text
    name: triage-bot
    description: Triage incoming issues and route them.
    model: opus
    tools: ['Edit', 'Grep', 'Read', 'mcp__github__create_pr', 'mcp__github__search_issues']
    body:
    ## main

    Triage incoming issues and route them.
    Use the linked board.

    ## fixer

    Apply the fix.
    ---
    map_tools: ['Edit', 'Grep', 'Read', 'mcp__github__create_pr', 'mcp__github__search_issues']
    model_alias(['claude-opus-4','sonnet']): opus
    model_alias('haiku-3.5'): haiku
    model_alias('mock'): inherit
    model_alias(None): inherit
    ```
