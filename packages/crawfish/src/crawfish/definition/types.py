"""Definition types — the code-first agent-team package (CRA-102).

A Definition is authored as a directory and compiled into this typed object (see
:mod:`crawfish.definition.compiler`). The team-coordination fields on ``TeamSpec``
(``coordination``/``lead``/``workspace``) and ``AgentSpec.delegates_to`` carry the
multi-agent topology (CRA-135); semantics are delegation-in / typed-result-out,
leaning on Claude's hierarchical subagent model — no bespoke message bus.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field

from crawfish.core.ids import new_id
from crawfish.core.types import Parameter, Policy
from crawfish.versioning.version import Freezable, Version

__all__ = [
    "Coordination",
    "AgentSpec",
    "TeamSpec",
    "Prompt",
    "DefinitionRef",
    "DefinitionAssets",
    "Definition",
    "MarketplacePackage",
    "MCPConnection",
]


class Coordination(str, Enum):
    SINGLE = "single"  # one agent (or independent agents), no coordinator
    LEAD = "lead"  # a lead delegates to subagents, combines typed results
    SEQUENTIAL = "sequential"  # agents run in declared order, output → input


class AgentSpec(BaseModel):
    """One agent in a team. ``prompt`` is compiled from its markdown body."""

    role: str
    prompt: str = ""
    # Model-universal by default (None -> platform picks). Pin to restrict THIS
    # agent. The runtime ships Claude-first (ADR 0005); the type stays universal.
    model: str | list[str] | None = None
    tools: list[str] = Field(default_factory=list)
    policies: list[str] = Field(default_factory=list)
    delegates_to: list[str] = Field(default_factory=list)  # subagent roles (CRA-135)
    context_strategy: str | None = None  # context-window strategy name (CRA-138)


class TeamSpec(BaseModel):
    agents: list[AgentSpec] = Field(default_factory=list)
    coordination: Coordination = Coordination.SINGLE
    lead: str | None = None  # coordinator role (for `lead` topology)
    workspace: Literal["shared", "isolated"] = "shared"


class Prompt(BaseModel):
    target: str
    text: str


class DefinitionRef(BaseModel):
    id: str
    version: str  # e.g. "0.2" or "0.1-sha"


class MCPConnection(BaseModel):
    """An MCP server connection authored in ``mcp/*.py`` (CRA-116).

    ``auth`` is a **secret reference** (an env-var name), never an inline credential —
    resolved at run time and injected into the server env, never into the prompt.
    ``tools`` lists the tool names the connection exposes (so the per-agent allowlist
    stays checkable).
    """

    name: str
    description: str = ""
    command: list[str] | None = None  # stdio transport: argv
    url: str | None = None  # http/sse transport
    auth: str | None = None  # secret reference (env var name) — by reference only
    tools: list[str] = Field(default_factory=list)  # exposed tool names


class DefinitionAssets(BaseModel):
    code: list[str] = Field(default_factory=list)  # python package modules
    mds: list[str] = Field(default_factory=list)
    plugins: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    mcp: list[MCPConnection] = Field(default_factory=list)
    policies: list[Policy] = Field(default_factory=list)


class MarketplacePackage(BaseModel):
    """Export shape (stub — full hub package lands with the registry, CRA-125)."""

    id: str
    version: str
    definition: dict[str, object]
    checksum: str


class Definition(Freezable):
    """The rigid, code-first agent-team package, compiled from a directory.

    Versioned and freezable (a frozen Definition is an immutable, reproducible
    artifact). ``id`` is set deterministically by the canonical loader (ADR 0006)
    so a directory and its installed package compile byte-identically.
    """

    id: str = Field(default_factory=new_id)
    team: TeamSpec = Field(default_factory=TeamSpec)
    injected_prompts: list[Prompt] = Field(default_factory=list)
    inputs: list[Parameter] = Field(default_factory=list)  # typed; each static|fluid
    outputs: list[Parameter] = Field(default_factory=list)
    dependencies: list[DefinitionRef] = Field(default_factory=list)
    assets: DefinitionAssets = Field(default_factory=DefinitionAssets)

    @classmethod
    def from_package(cls, path: str) -> Definition:
        """Compile + validate a directory into a Definition (canonical loader)."""
        from crawfish.definition.compiler import load_definition

        return load_definition(path)

    def export(self) -> MarketplacePackage:
        """Export to a marketplace package shape."""
        import hashlib

        payload = self.model_dump(mode="json")
        blob = repr(sorted(payload.items())).encode()
        checksum = hashlib.sha256(blob).hexdigest()[:16]
        return MarketplacePackage(
            id=self.id, version=str(self.version), definition=payload, checksum=checksum
        )

    def agent(self, role: str) -> AgentSpec | None:
        return next((a for a in self.team.agents if a.role == role), None)


# keep Version importable from here for the loader's convenience
_ = Version
