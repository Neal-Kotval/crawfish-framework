"""The directory compiler — files in, typed ``Definition`` out (CRA-102).

One **canonical loader** (:func:`load_definition`) backs both ``from_package(path)``
and the installed-package / ``DefinitionRef`` route (CRA-113), so a directory and its
installed package compile to byte-identical Definitions (ADR 0006). Identity is
content-derived, never path- or time-derived.

Compile contract (see the spec table):

* ``instructions.md`` (+ ``agents/*.md``) → ``TeamSpec.agents``
* ``tools/*.py`` → tool name = filename stem (no registration)
* ``skills/*.md`` / ``mcp/*.py`` / ``policies/*.py`` → ``DefinitionAssets``
* typed IO + deps in ``definition.py`` → ``inputs``/``outputs``/``dependencies``

Broken bindings (an agent referencing an unknown tool/policy) fail at **load time**.

Security note: compiling imports ``definition.py``/``policies/*.py``/``tools/*.py`` —
authoring-time trusted code. Out-of-process execution of host-side tool code at *run*
time, with taint propagation, is CRA-114.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import tomllib
from pathlib import Path
from typing import Any, Literal

import yaml

from crawfish.core.types import Parameter, Policy
from crawfish.definition.types import (
    AgentSpec,
    Coordination,
    Definition,
    DefinitionAssets,
    DefinitionRef,
    MCPConnection,
    Prompt,
    TeamSpec,
)
from crawfish.versioning.version import Version

__all__ = ["load_definition", "DefinitionLoadError"]


class DefinitionLoadError(Exception):
    """Raised when a directory cannot compile to a valid Definition."""


# Files/dirs excluded from the content hash so writing the lock or caches doesn't
# change identity on recompile.
_HASH_EXCLUDE = {"definition.lock", "__pycache__", ".crawfish"}


def _parse_front_matter(text: str) -> tuple[dict[str, Any], str]:
    """Split YAML front-matter (``---`` fenced) from a markdown body."""
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            raw = text[3:end].strip("\n")
            data = yaml.safe_load(raw) or {}
            if not isinstance(data, dict):
                raise DefinitionLoadError("front-matter must be a mapping")
            body = text[end + 4 :].lstrip("\n")
            return data, body
    return {}, text


def _import_module(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise DefinitionLoadError(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # surface authoring errors at load time
        raise DefinitionLoadError(f"error importing {path.name}: {exc}") from exc
    return module


def _content_sha(root: Path) -> str:
    h = hashlib.sha256()
    files = sorted(
        p
        for p in root.rglob("*")
        if p.is_file() and not any(part in _HASH_EXCLUDE for part in p.relative_to(root).parts)
    )
    for p in files:
        h.update(str(p.relative_to(root)).encode())
        h.update(b"\0")
        h.update(p.read_bytes())
        h.update(b"\0")
    return h.hexdigest()[:12]


def _agent_from_md(role: str, text: str) -> AgentSpec:
    fm, body = _parse_front_matter(text)
    return AgentSpec(
        role=fm.get("role", role),
        prompt=body.strip(),
        model=fm.get("model"),
        tools=list(fm.get("tools", [])),
        policies=list(fm.get("policies", [])),
        delegates_to=list(fm.get("delegates_to", [])),
    )


def load_definition(path: str | Path) -> Definition:
    root = Path(path)
    if not root.is_dir():
        raise DefinitionLoadError(f"not a directory: {root}")

    instructions = root / "instructions.md"
    definition_py = root / "definition.py"
    if not instructions.exists() and not definition_py.exists():
        raise DefinitionLoadError("a Definition needs at least instructions.md or definition.py")

    # -- discover tools (filename stem == tool name; must be callable) --------
    tool_names: list[str] = []
    for tool_file in sorted((root / "tools").glob("*.py")):
        stem = tool_file.stem
        if stem.startswith("_"):
            continue
        module = _import_module(tool_file, f"_craw_tool_{stem}")
        if not callable(getattr(module, stem, None)):
            raise DefinitionLoadError(
                f"tools/{tool_file.name} must define a callable named {stem!r}"
            )
        tool_names.append(stem)

    # -- discover policies (module-level Policy instances) --------------------
    policies: list[Policy] = []
    for pol_file in sorted((root / "policies").glob("*.py")):
        if pol_file.stem.startswith("_"):
            continue
        module = _import_module(pol_file, f"_craw_policy_{pol_file.stem}")
        for value in vars(module).values():
            if isinstance(value, Policy):
                policies.append(value)
    policy_names = {p.name for p in policies}

    skills = sorted(p.name for p in (root / "skills").glob("*.md"))

    # -- discover MCP connections (module-level MCPConnection instances) ------
    mcp_connections: list[MCPConnection] = []
    for mcp_file in sorted((root / "mcp").glob("*.py")):
        if mcp_file.stem.startswith("_"):
            continue
        module = _import_module(mcp_file, f"_craw_mcp_{mcp_file.stem}")
        for value in vars(module).values():
            if isinstance(value, MCPConnection):
                mcp_connections.append(value)
    mcp_tool_names = [t for conn in mcp_connections for t in conn.tools]
    all_tool_names = list(tool_names) + mcp_tool_names  # local + MCP-provided tools

    # -- build the team ------------------------------------------------------
    agents: list[AgentSpec] = []
    if instructions.exists():
        agents.append(_agent_from_md("main", instructions.read_text()))
    for agent_file in sorted((root / "agents").glob("*.md")):
        agents.append(_agent_from_md(agent_file.stem, agent_file.read_text()))

    # tools with no explicit per-agent restriction get all available (no wiring):
    # local tools + connected MCP tools.
    for spec in agents:
        if not spec.tools:
            spec.tools = list(all_tool_names)

    # -- definition.py: typed IO, deps, optional team/version override -------
    inputs: list[Parameter] = []
    outputs: list[Parameter] = []
    injected_prompts: list[Prompt] = []
    dependencies: list[DefinitionRef] = []
    team_override: TeamSpec | None = None
    version: Version | None = None
    coordination: Coordination | None = None
    lead: str | None = None
    workspace: Literal["shared", "isolated"] = "shared"

    if definition_py.exists():
        module = _import_module(definition_py, "_craw_definition")
        inputs = list(getattr(module, "inputs", []) or [])
        outputs = list(getattr(module, "outputs", []) or [])
        injected_prompts = list(getattr(module, "injected_prompts", []) or [])
        dependencies = list(getattr(module, "dependencies", []) or [])
        team_override = getattr(module, "team", None)
        version = getattr(module, "version", None)
        coordination = getattr(module, "coordination", None)
        lead = getattr(module, "lead", None)
        workspace = getattr(module, "workspace", "shared")

    if team_override is not None:
        team = team_override
    else:
        coord = coordination or (
            Coordination.LEAD if (lead and len(agents) > 1) else Coordination.SINGLE
        )
        team = TeamSpec(agents=agents, coordination=coord, lead=lead, workspace=workspace)

    # -- pyproject: name (identity) + version + deps -------------------------
    name = root.name
    pyproject = root / "pyproject.toml"
    if pyproject.exists():
        meta = tomllib.loads(pyproject.read_text()).get("project", {})
        name = meta.get("name", name)
        if version is None and "version" in meta:
            parts = str(meta["version"]).split(".")
            version = Version(major=int(parts[0]), minor=int(parts[1]) if len(parts) > 1 else 0)

    sha = _content_sha(root)
    if version is None:
        version = Version(sha=sha)
    else:
        version = Version(major=version.major, minor=version.minor, sha=sha)

    # -- validate bindings (fail at load) ------------------------------------
    available_tools = set(all_tool_names)
    for spec in team.agents:
        for tool in spec.tools:
            if tool not in available_tools:
                raise DefinitionLoadError(
                    f"agent {spec.role!r} binds unknown tool {tool!r} "
                    f"(available: {sorted(available_tools)})"
                )
        for pol in spec.policies:
            if pol not in policy_names:
                raise DefinitionLoadError(
                    f"agent {spec.role!r} binds unknown policy {pol!r} "
                    f"(available: {sorted(policy_names)})"
                )
        for sub in spec.delegates_to:
            if sub not in {a.role for a in team.agents}:
                raise DefinitionLoadError(f"agent {spec.role!r} delegates to unknown role {sub!r}")

    assets = DefinitionAssets(
        code=[definition_py.name] if definition_py.exists() else [],
        mds=([instructions.name] if instructions.exists() else [])
        + [f"agents/{a.name}" for a in sorted((root / "agents").glob("*.md"))],
        skills=skills,
        mcp=mcp_connections,
        policies=policies,
    )

    definition = Definition(
        id=name,
        version=version,
        team=team,
        injected_prompts=injected_prompts,
        inputs=inputs,
        outputs=outputs,
        dependencies=dependencies,
        assets=assets,
    )

    _write_lock(root, definition)
    return definition


def _write_lock(root: Path, definition: Definition) -> None:
    """Write ``definition.lock`` — pinned deps + content sha, for reproducibility."""
    lock = {
        "id": definition.id,
        "version": str(definition.version),
        "dependencies": {d.id: d.version for d in definition.dependencies},
    }
    (root / "definition.lock").write_text(json.dumps(lock, indent=2, sort_keys=True) + "\n")
