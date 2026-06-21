# Authoring — config, discovery, scaffold & doctor

How a Crawfish *project on disk* becomes something the engine can run: the
`crawfish.toml` manifest that describes it, the scan that finds the units you
authored, the scaffolder that lays out a fresh one, and the doctor that checks
its structure. These live in `crawfish.config`, `crawfish.discovery`,
`crawfish.scaffold`, and `crawfish.doctor`.

**Symbols on this page:** `ProfileConfig` · `ProjectManifest` · `ProjectPaths` ·
`load_manifest` · `load_models_config` · `ModelsConfigError` · `Registry` ·
`UnitRef` · `scaffold_project` · `DoctorFinding` · `DoctorReport` · `diagnose`

---

## Core

A **Crawfish project** is a directory you author by hand. Its root holds the
folders that make up your agents — `definitions/`, `sources/`, `sinks/`, and a
few more — alongside one manifest file, `crawfish.toml`. A hidden `.crawfish/`
folder holds **generated state** (anything the engine writes for itself);
everything else is **authored** (what you wrote and check into git).

The **manifest** (`crawfish.toml`) names the project and picks its defaults.
`load_manifest` reads it into a `ProjectManifest` — and if the file is absent,
hands back a manifest of pure defaults, so a bare directory still works.

A **profile** is a named runtime choice. `dev` runs the agent loop by shelling
out to `claude -p` with no API key; `prod` runs it on the managed backend. A
`ProfileConfig` records which runtime a profile uses plus any free-form settings.
The manifest's `default_profile` says which one to use when you don't name one.

`ProjectPaths` records **where each kind of unit lives** — `sources/` for
sources, `definitions/` for agent teams, and so on. The defaults are the
canonical layout; a project can relocate any folder, and every other tool here
follows the override rather than assuming the default name.

A project's model choices live in a `[models]` block: a `default` model for
agents that don't pin one, and **aliases** — friendly names like `fast` that map
to a concrete model id. `load_models_config` reads just that block. If the block
is malformed — a non-string default, an alias pointing at another alias — it
raises `ModelsConfigError` at load time, so the project fails fast with a clear
message instead of a confusing failure later.

**Discovery** is the scan that finds your units. A `Registry` collects
`UnitRef`s — one per discovered unit, recording its `kind` (source, sink,
definition…), its `name`, and where it came from. It gathers from two feeds:
installed `crawfish-*` packages (via Python *entry points* — the standard way a
package advertises plug-ins) and a scan of the local project folders. When two
units claim the same kind and name, the **first one registered wins** and a
warning is emitted.

`scaffold_project` writes a fresh project to disk — manifest, a working example
agent (the triage-bot), a `.gitignore`, fixtures — so one command produces a
runnable project with no API key.

`diagnose` is the health check behind `craw doctor`. It walks the project and
returns a `DoctorReport`: a list of `DoctorFinding`s, each tagged with a `level`
(`ok`, `info`, `warn`, or `error`). The report is healthy when nothing rose above
`info`.

---

## Ramps up

### The manifest is all-optional, defaults all the way down

Every field on `ProjectManifest` has a default, and `load_manifest` returns a
fully-defaulted manifest when `crawfish.toml` is missing. This is deliberate: a
directory with nothing but a few `.py` files is still a valid project the engine
can reason about. The TOML is read with the stdlib `tomllib`; the `[project]`,
`[profiles.*]`, and `[models]` tables map onto `ProjectManifest`,
`ProfileConfig`, and `ModelsConfig` respectively.

`resolve_profile` resolves a profile name in three steps: an explicit entry under
`[profiles.*]` wins; otherwise a built-in name (`dev` → `command`, `prod` →
`managed`, from `DEFAULT_PROFILES`) is synthesised; otherwise it raises
`KeyError`. So you get working `dev`/`prod` profiles for free without declaring
them, but can override either in the manifest.

### Why an alias may not point at another alias

`resolve_model` (see [providers](providers.md)) expands an alias by a **single
hop** — it looks the name up once and returns the target. An alias chain
(`a → b → c`) would therefore resolve `a` to the *name* `b`, not to a real model.
Rather than let that surface as a baffling runtime error, `_models_config_from_raw`
rejects any alias whose target is itself an alias, raising `ModelsConfigError` at
load time. The same function rejects a non-string `default`, a non-table
`[models.aliases]`, a non-string alias target, and an `allowed_providers` that
isn't a list of strings.

`load_models_config` and `load_manifest` share this builder, so the validation is
identical whether you load the whole manifest or just the models block. Both
return an empty/default config when the file or section is absent — the
back-compat path where the runtime's built-in model fallback applies.

### Two feeds, first-wins, namespaced by kind

`Registry.discover` runs the two feeds in order: **entry points first**, then the
**local directory scan**. Because first registration wins, an installed package's
unit beats a local file of the same kind and name — and the collision warns so
you know a local file was shadowed. Collisions are scoped to a `(kind, name)`
pair, so a `source` and a `sink` may share a name freely.

The local scan treats two kinds — `definition` and `observer` — as **directory
packages**: a subfolder counts as a unit when it contains an `instructions.md` or
a `definition.py`. Every other kind is a **single file**: a `*.py` whose stem
doesn't start with `_` becomes a unit named for the file. When called without an
explicit `paths` map, `discover` loads the manifest and honours any
`[project.paths]` relocation automatically. See [definition](definition.md) for
the units the registry discovers.

### What `diagnose` actually checks

`diagnose` produces findings in a fixed order, applying any `[project.paths]`
override so the report matches the real project:

1. **Manifest presence.** `crawfish.toml` present → `ok`; absent → `info` (the
   default layout is assumed).
2. **Each unit folder**, in `ProjectPaths` field order. Present → `ok` (with a
   `(relocated from …/)` note if the path was overridden). A folder that an
   override *points at but that doesn't exist* → `warn` (a real
   misconfiguration). A default folder that's simply absent → `info` (optional).
3. **Misplacement.** A Definition-shaped subfolder (one with `instructions.md` or
   `definition.py`) sitting under a non-definition root → `warn`, with a hint to
   move it to `definitions/`. The `definitions` and `observers` roots are
   exempted, since directory packages belong there.
4. **Generated-vs-authored separation.** If `.crawfish/` exists → `ok`. If it
   isn't gitignored → `warn`. If any authored unit folder is found *inside*
   `.crawfish/` → `error` (authored code must not hide in generated state).

`DoctorReport.ok` is `True` when no finding is `warn` or `error`. `DoctorReport.text()`
renders the findings with per-level glyphs and a one-line verdict.

---

## API reference

### `ProfileConfig`

`class ProfileConfig(BaseModel)` — one named profile.

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `runtime` | `str` | `"command"` | Runtime backend name (e.g. `"command"`, `"managed"`). |
| `settings` | `dict[str, object]` | `{}` | Free-form profile settings. |

### `ProjectPaths`

`class ProjectPaths(BaseModel)` — where each unit kind lives, relative to the
project root. A project may relocate any folder via `crawfish.toml [project.paths]`.

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `sources` | `str` | `"sources"` | Source units. |
| `sinks` | `str` | `"sinks"` | Sink units. |
| `definitions` | `str` | `"definitions"` | Definition packages (agent teams). |
| `pipelines` | `str` | `"pipelines"` | Pipeline wiring. |
| `observers` | `str` | `"observers"` | Observer units. |
| `tools` | `str` | `"tools"` | Custom tool functions. |
| `policies` | `str` | `"policies"` | Reusable policies. |

`as_discovery_map() -> dict[str, str]` returns `{unit-kind: subdir}` for the
registry's local scan. It maps `source`, `sink`, `definition`, `observer`,
`tool`, and `policy` — **`pipelines` is not included**, so the local scan does
not discover pipeline files by this map.

### `ProjectManifest`

`class ProjectManifest(BaseModel)` — the parsed `crawfish.toml`.

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `name` | `str` | `"crawfish-project"` | Project name. |
| `version` | `str` | `"0.1.0"` | Project version. |
| `default_profile` | `str` | `"dev"` | Profile used when none is named. |
| `paths` | `ProjectPaths` | `ProjectPaths()` | Folder layout. |
| `profiles` | `dict[str, ProfileConfig]` | `{}` | Named profile overrides. |
| `models` | `ModelsConfig` | `ModelsConfig()` | Model default + aliases ([providers](providers.md)). |

```python
def resolve_profile(self, name: str | None = None) -> ProfileConfig
```

Resolves `name` (or `default_profile`) against `profiles`, then the built-in
`DEFAULT_PROFILES` (`dev` → `command`, `prod` → `managed`). Raises `KeyError` for
an unknown name.

### `load_manifest`

```python
def load_manifest(project_dir: str | Path = ".") -> ProjectManifest
```

Loads `crawfish.toml` from `project_dir`. Returns a fully-defaulted
`ProjectManifest` when the file is absent.

### `load_models_config`

```python
def load_models_config(project_dir: str | Path = ".") -> ModelsConfig
```

Loads only the `[models]` section as a frozen `ModelsConfig`. Returns an empty
config (no default, no aliases, open policy) when the file or section is absent.
Raises `ModelsConfigError` on a malformed block.

### `ModelsConfigError`

`class ModelsConfigError(ValueError)` — a malformed `[models]` section. Raised at
config-load time for: a non-string `default`; a non-table `[models.aliases]`; a
non-string alias target; an alias pointing at another alias; or an
`allowed_providers` that isn't a list of strings.

### `UnitRef`

`@dataclass UnitRef` — one discovered unit.

| Field | Type | Notes |
| --- | --- | --- |
| `kind` | `str` | `"source"`, `"sink"`, `"definition"`, `"observer"`, `"tool"`, `"policy"`, or `"type"`. |
| `name` | `str` | Unit name (entry-point name, or file/dir stem). |
| `origin` | `str` | `"entrypoint:<group>"` or `"local:<path>"`. |
| `target` | `str` | Entry-point value, or filesystem path. |

### `Registry`

`@dataclass Registry` — collects discovered units; first `(kind, name)` wins.

| Member | Signature | Notes |
| --- | --- | --- |
| `units` | `dict[tuple[str, str], UnitRef]` | Keyed by `(kind, name)`. |
| `register` | `(ref: UnitRef) -> bool` | `False` + warns on a `(kind, name)` collision (keeps the existing). |
| `of_kind` | `(kind: str) -> list[UnitRef]` | All units of one kind. |
| `get` | `(kind: str, name: str) -> UnitRef \| None` | Lookup, or `None`. |
| `discover_entry_points` | `() -> None` | Scans the four `ENTRY_POINT_GROUPS`. |
| `discover_local` | `(project_dir, paths=None) -> None` | Scans local folders; `paths` overrides win. |
| `discover` (classmethod) | `(project_dir=".", paths=None) -> Registry` | Entry points first, then local; loads `[project.paths]` when `paths is None`. |

`ENTRY_POINT_GROUPS` maps `source`/`sink`/`definition`/`type` to the
`crawfish.sources`/`.sinks`/`.definitions`/`.types` groups. `LOCAL_DIRS` is the
default folder map for the local scan. The kinds `definition` and `observer` are
discovered as directory packages (a subfolder with `instructions.md` or
`definition.py`); all others as `*.py` files whose stem doesn't start with `_`.

### `scaffold_project`

```python
def scaffold_project(name: str = "crawfish-app") -> Path
```

Creates a self-contained project directory at `name` (relative to the cwd) and
returns its `Path`. Writes every entry of the `FILES` template: `crawfish.toml`,
`.env.example`, `.gitignore`, `README.md`, the `definitions/triage-bot/` example
package, a fixture, and `.gitkeep`s for `sources/`/`sinks/`. Uses
`exist_ok=True`, so it overlays onto an existing directory.

### `DoctorFinding`

`class DoctorFinding(BaseModel)` — one health observation.

| Field | Type | Notes |
| --- | --- | --- |
| `level` | `str` | `"ok"` \| `"info"` \| `"warn"` \| `"error"`. |
| `message` | `str` | Human-readable finding. |

### `DoctorReport`

`class DoctorReport(BaseModel)`.

| Member | Signature | Notes |
| --- | --- | --- |
| `findings` | `list[DoctorFinding]` | Default `[]`. |
| `ok` (property) | `bool` | `True` when no finding is `warn`/`error`. |
| `add` | `(level: str, message: str) -> None` | Append a finding. |
| `text` | `() -> str` | Glyph-rendered lines (`✓ · ! ✗`) plus a verdict line. |

### `diagnose`

```python
def diagnose(project_dir: str | Path = ".") -> DoctorReport
```

Inspects `project_dir` and returns a structure-health `DoctorReport`. Checks, in
order: manifest presence; each unit folder (present / overridden-but-missing /
optional-absent); misplaced Definition-shaped folders; and the
authored-vs-generated split under `.crawfish/`. Honours `[project.paths]`
overrides. `GENERATED_DIR` is `".crawfish"`; `CANONICAL_LAYOUT` maps each folder
to a one-line description.

---

## Example

Scaffold a fresh project into a temp directory, read its manifest back, discover
its units, and run the doctor — pure local filesystem, no network.

```python
import tempfile, os
from crawfish.scaffold import scaffold_project
from crawfish.config import load_manifest
from crawfish.discovery import Registry
from crawfish.doctor import diagnose

with tempfile.TemporaryDirectory() as tmp:
    os.chdir(tmp)
    root = scaffold_project("crawfish-app")          # written under tmp

    m = load_manifest(root)
    print("name:", m.name)
    print("default_profile:", m.default_profile)
    print("runtime(dev):", m.resolve_profile("dev").runtime)
    print("models.default:", m.models.default)
    print("alias fast:", m.models.aliases["fast"])

    reg = Registry.discover(root)
    print("definitions found:", [r.name for r in reg.of_kind("definition")])

    report = diagnose(root)
    print("findings:", len(report.findings))
    print("ok:", report.ok)
    print("levels:", sorted({f.level for f in report.findings}))
```

??? success "▶ Output"

    ```text
    name: crawfish-app
    default_profile: dev
    runtime(dev): command
    models.default: claude-opus-4-8
    alias fast: claude-haiku-4-5
    definitions found: ['triage-bot']
    findings: 8
    ok: True
    levels: ['info', 'ok']
    ```
