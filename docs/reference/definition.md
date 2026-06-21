# Definition

The authored, on-disk spec of an agent or team — the directory you write and the
typed object it compiles into. A Definition fixes the team topology, the typed
inputs/outputs, the prompts, and the bundled assets (tools, skills, MCP servers,
policies) for one reproducible package. These live in `crawfish.definition`.

**Symbols on this page:** `Definition` · `AgentSpec` · `TeamSpec` · `Coordination` ·
`Prompt` · `DefinitionRef` · `DefinitionAssets` · `MarketplacePackage` ·
`MCPConnection` · `load_definition` · `DefinitionLoadError`

---

## Core

A **Definition** is one agent or team, written as a directory of files and compiled
into a single typed object. The directory holds an `instructions.md` (the main
agent's prompt), optional `agents/*.md` (more agents), `tools/*.py`, `skills/*.md`,
`mcp/*.py`, `policies/*.py`, and a `definition.py` that declares typed inputs and
outputs. The compiler reads those files and produces a `Definition` — the unit you
install, version, and run.

A team is a list of **agents**. Each agent is an `AgentSpec`: a `role` name, a
`prompt` (its instructions), the `tools` and `policies` it may use, and the roles it
may hand work to (`delegates_to`). The whole team is a `TeamSpec`: the agent list
plus how they work together.

That "how" is the **`Coordination`** setting — one of three shapes:

- **single** — one agent, or several independent agents, with no coordinator.
- **lead** — a designated `lead` agent delegates to subagents and combines their
  typed results.
- **sequential** — agents run in declared order, each one's output feeding the next.

A Definition also carries **typed inputs and outputs** — `Parameter`s, the same
typed slots used everywhere in Crawfish, each marked *static* (set once) or *fluid*
(varies per item, and treated as untrusted data). It carries **injected prompts**
(`Prompt` — extra text aimed at a named target), **dependencies** on other
definitions (`DefinitionRef` — an `id` plus a `version`), and an **assets** bundle
(`DefinitionAssets`) listing the code modules, skills, MCP connections, and policies
the directory contributed.

An **`MCPConnection`** describes one external tool server (an MCP server) the
definition connects to. Its `auth` is always a *secret reference* — the name of an
environment variable — never an inline password, so credentials are resolved at run
time and never written into a prompt.

You compile a directory with **`load_definition`** (the one canonical loader). On any
problem — a missing directory, an agent binding a tool that doesn't exist, a broken
import — it raises **`DefinitionLoadError`**. A finished Definition can be exported to
a **`MarketplacePackage`**, a flat, checksummed shape for publishing to a hub.

---

## Ramps up

### One canonical loader, deterministic identity

`load_definition` is the *single* loader: `Definition.from_package(path)` calls it,
and the installed-package route resolves through it too. A directory and its installed
copy therefore compile to byte-identical Definitions. Identity is **content-derived,
never path- or time-derived** — the version's content hash (`sha`) is computed over
the directory's files, with the lockfile, caches, VCS dirs, and virtualenvs excluded
(`definition.lock`, `__pycache__`, `.crawfish`, `uv.lock`, `.venv`, `.env`, `.git`,
`node_modules`, `.DS_Store`, `.claude`). The Definition's `id` is set from the
package name (`pyproject.toml` `project.name`, else the directory name), not the
random default. See
[ADR 0006](../architecture/decisions/0006-canonical-loader-deterministic-identity.md).

### Bindings are validated at load time

The compiler fails fast. After discovering tools (each `tools/*.py` must define a
callable whose name matches the filename stem), policies (module-level `Policy`
instances in `policies/*.py`), and MCP connections (module-level `MCPConnection`
instances in `mcp/*.py`), it checks every agent:

- a tool an agent binds must exist (local tool **or** a tool exposed by a connected
  MCP server), else `DefinitionLoadError`;
- a policy an agent binds must exist among the discovered policies;
- every role in `delegates_to` must be a real agent role in the same team.

An agent that declares no `tools` is given **all** available tools (local + MCP) — no
explicit wiring needed. Compiling imports `definition.py`, `policies/*.py`, and
`tools/*.py`: this is authoring-time trusted code. Host-side *tool* code runs
out-of-process at run time with taint propagation (untrusted fluid data is tracked as
it flows), not here.

### Team coordination is hierarchical

The coordination model is delegation-in / typed-result-out, leaning on Claude's
hierarchical subagent model — there is no bespoke message bus. If `definition.py`
sets neither a full `team` override nor an explicit `coordination`, the compiler
infers it: a `lead` set with more than one agent ⇒ `Coordination.LEAD`, otherwise
`Coordination.SINGLE`. `workspace` is `"shared"` by default (agents see one
workspace) or `"isolated"`. See
[ADR 0007](../architecture/decisions/0007-team-coordination-hierarchical.md).

### Definitions are versioned and freezable

`Definition` subclasses `Freezable`: a frozen Definition is an immutable, reproducible
artifact that rejects mutation. Its `version` is a `Version` (major/minor + content
`sha`); a `pyproject.toml` `project.version` like `"0.2"` sets major/minor, and the
loader always stamps the freshly computed `sha`. `export()` produces a
`MarketplacePackage` whose `checksum` is a 16-char SHA-256 over the sorted,
JSON-dumped payload — stable for the same content. See
[ADR 0012](../architecture/decisions/0012-definitions-are-versioned.md).

### Model pinning stays universal

`AgentSpec.model` defaults to `None`, meaning model-universal — the platform picks.
Pin a string or list to restrict that one agent. The runtime ships Claude-first, but
the type stays universal by design.

---

## API reference

### `Coordination`

`class Coordination(str, Enum)` — how a team's agents work together.

| Member | Value | Meaning |
| --- | --- | --- |
| `Coordination.SINGLE` | `"single"` | One agent, or independent agents, no coordinator. |
| `Coordination.LEAD` | `"lead"` | A lead delegates to subagents and combines typed results. |
| `Coordination.SEQUENTIAL` | `"sequential"` | Agents run in declared order, output → input. |

### `AgentSpec`

`class AgentSpec(BaseModel)` — one agent in a team; `prompt` is compiled from its
markdown body.

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `role` | `str` | — (required) | The agent's role name. |
| `prompt` | `str` | `""` | Instructions, compiled from the markdown body. |
| `model` | `str \| list[str] \| None` | `None` | `None` = model-universal (platform picks). Pin to restrict this agent. |
| `tools` | `list[str]` | `[]` | Tool names this agent may use. Empty ⇒ compiler grants all available. |
| `policies` | `list[str]` | `[]` | Policy names this agent is bound by. |
| `delegates_to` | `list[str]` | `[]` | Subagent roles this agent may delegate to. |
| `context_strategy` | `str \| None` | `None` | Named context-window strategy. |

### `TeamSpec`

`class TeamSpec(BaseModel)` — the team's agents plus their coordination.

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `agents` | `list[AgentSpec]` | `[]` | The agents in the team. |
| `coordination` | `Coordination` | `Coordination.SINGLE` | How agents work together. |
| `lead` | `str \| None` | `None` | Coordinator role (used by the `lead` topology). |
| `workspace` | `Literal["shared", "isolated"]` | `"shared"` | Whether agents share one workspace. |
| `context_carry` | `str \| None` | `None` | Which subset of the typed Context artifact carries between agents (e.g. `full` / `recency` / `summary` / `typed_fields`). `None` ⇒ lossless default. |

### `Prompt`

`class Prompt(BaseModel)` — a piece of injected prompt text aimed at a named target.

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `target` | `str` | — (required) | What the text is injected into. |
| `text` | `str` | — (required) | The prompt text. |

### `DefinitionRef`

`class DefinitionRef(BaseModel)` — a reference to another definition (a dependency).

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `id` | `str` | — (required) | The referenced definition's id. |
| `version` | `str` | — (required) | Version string, e.g. `"0.2"` or `"0.1-sha"`. |

### `MCPConnection`

`class MCPConnection(BaseModel)` — one MCP server connection authored in `mcp/*.py`.

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `name` | `str` | — (required) | Connection name. |
| `description` | `str` | `""` | Human description. |
| `command` | `list[str] \| None` | `None` | stdio transport: the server argv. |
| `url` | `str \| None` | `None` | http/sse transport URL. |
| `auth` | `str \| None` | `None` | **Secret reference** — an env-var name, by reference only. Never an inline credential; injected into the server env, never the prompt. |
| `tools` | `list[str]` | `[]` | Tool names this connection exposes (keeps the per-agent allowlist checkable). |

### `DefinitionAssets`

`class DefinitionAssets(BaseModel)` — the assets a directory contributed.

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `code` | `list[str]` | `[]` | Python package modules (e.g. `definition.py`). |
| `mds` | `list[str]` | `[]` | Markdown files (`instructions.md`, `agents/*.md`). |
| `plugins` | `list[str]` | `[]` | Plugin names. |
| `skills` | `list[str]` | `[]` | Skill file names from `skills/*.md`. |
| `mcp` | `list[MCPConnection]` | `[]` | Discovered MCP connections. |
| `policies` | `list[Policy]` | `[]` | Discovered policies. |

### `MarketplacePackage`

`class MarketplacePackage(BaseModel)` — the export shape for publishing (stub; full
hub package lands with the registry).

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `id` | `str` | — (required) | Package id (the Definition's `id`). |
| `version` | `str` | — (required) | Version string. |
| `definition` | `dict[str, object]` | — (required) | The JSON-dumped Definition payload. |
| `checksum` | `str` | — (required) | 16-char SHA-256 over the sorted payload. |

### `Definition`

`class Definition(Freezable)` — the rigid, code-first agent-team package, compiled
from a directory. Versioned and freezable; a frozen Definition is immutable.

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `id` | `str` | `new_id()` | Set deterministically by the loader (package/dir name). |
| `team` | `TeamSpec` | `TeamSpec()` | The agent team. |
| `injected_prompts` | `list[Prompt]` | `[]` | Extra prompt text aimed at named targets. |
| `inputs` | `list[Parameter]` | `[]` | Typed inputs; each static or fluid. |
| `outputs` | `list[Parameter]` | `[]` | Typed outputs. |
| `dependencies` | `list[DefinitionRef]` | `[]` | Other definitions this one depends on. |
| `version` | `Version` | (from `Freezable`) | Major/minor + content `sha`. |
| `assets` | `DefinitionAssets` | `DefinitionAssets()` | Contributed code, skills, MCP, policies. |

Methods:

```python
@classmethod
def from_package(cls, path: str) -> Definition   # compile a directory (calls load_definition)
def export(self) -> MarketplacePackage           # to the publish shape; 16-char checksum
def agent(self, role: str) -> AgentSpec | None    # find an agent by role, or None
```

### `load_definition`

```python
def load_definition(path: str | Path) -> Definition
```

The canonical loader. Compiles and validates a directory into a `Definition`,
inferring coordination, discovering assets, validating every binding, and writing
`definition.lock`. Requires at least an `instructions.md` or a `definition.py`. Raises
`DefinitionLoadError` on any failure.

### `DefinitionLoadError`

`class DefinitionLoadError(Exception)` — raised when a directory cannot compile to a
valid Definition (not a directory, missing entry files, an unknown tool/policy/role
binding, a bad front-matter mapping, or an import error in authored code).

---

## Example

Build a two-agent `lead`-coordinated Definition in memory, read its key fields, export
it, and trigger a `DefinitionLoadError` on a bad load — all deterministic, no runtime.

```python
from crawfish.definition.types import (
    AgentSpec, TeamSpec, Coordination, Definition, DefinitionRef,
)
from crawfish.core.types import Parameter, Flow
from crawfish.definition.compiler import load_definition, DefinitionLoadError

# A lead that delegates to a researcher.
lead = AgentSpec(role="lead", prompt="Triage the inbox.", delegates_to=["researcher"])
researcher = AgentSpec(role="researcher", tools=["search"])
team = TeamSpec(agents=[lead, researcher], coordination=Coordination.LEAD, lead="lead")

defn = Definition(
    id="triage-bot",
    team=team,
    inputs=[Parameter(name="ticket", type="str", flow=Flow.FLUID)],
    outputs=[Parameter(name="label", type="str")],
    dependencies=[DefinitionRef(id="shared-tools", version="0.2")],
)

print(defn.id, defn.team.coordination.value, defn.team.lead)
print(defn.team.workspace, "agents:", len(defn.team.agents))
print("lead delegates_to:", defn.agent("lead").delegates_to)
print("researcher tools:", defn.agent("researcher").tools)
print("input flow:", defn.inputs[0].flow.value, "| dep:",
      defn.dependencies[0].id, defn.dependencies[0].version)

# Export to the marketplace shape (16-char checksum over the dumped payload).
print("export checksum len:", len(defn.export().checksum))

# A bad load raises DefinitionLoadError.
try:
    load_definition("/tmp/does-not-exist-crawfish")
except DefinitionLoadError as exc:
    print("DefinitionLoadError:", exc)
```

??? success "▶ Output"

    ```text
    triage-bot lead lead
    shared agents: 2
    lead delegates_to: ['researcher']
    researcher tools: ['search']
    input flow: fluid | dep: shared-tools 0.2
    export checksum len: 16
    DefinitionLoadError: not a directory: /tmp/does-not-exist-crawfish
    ```
