"""Definition types — the code-first agent-team package.

A Definition is authored as a directory and compiled into this typed object (see
:mod:`crawfish.definition.compiler`). The team-coordination fields on ``TeamSpec``
(``coordination``/``lead``/``workspace``) and ``AgentSpec.delegates_to`` carry the
multi-agent topology; semantics are delegation-in / typed-result-out,
leaning on Claude's hierarchical subagent model — no bespoke message bus.
"""

from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, Field

from crawfish.core.ids import new_id
from crawfish.core.types import Parameter, Policy
from crawfish.versioning.version import Freezable, Version

if TYPE_CHECKING:
    from contextlib import AbstractContextManager

    from crawfish.borrow import Borrow
    from crawfish.store.base import Store

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
    "CONTENT_HASH_VERSION",
    "DECODE_KNOB_FIELDS",
]

# Bumped whenever the set of fields that enter a Definition's content hash changes.
# v1 added the tunable decode knobs (temperature/top_p/sample_k) to AgentSpec (ADR 0017,
# F-5). The decode-knob fields are *hash-neutral when None* (see ``Definition.content_dict``):
# an unmigrated artifact that never set them keeps its pre-v1 sha byte-for-byte. An artifact
# that pins any knob gets a new sha and must be re-frozen. See docs/_changelog/F-5.md.
CONTENT_HASH_VERSION = 1

# The tunable decode knobs the Tuner searches and ``state_dict`` serializes. They are the
# ONE authoritative location for these parameters (ADR 0017): ``RunRequest.temperature`` is
# *derived* from the resolved Definition, never independently set. Excluded from the content
# hash when None so adding them does not perturb existing frozen artifacts.
DECODE_KNOB_FIELDS = ("temperature", "top_p", "sample_k")


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
    delegates_to: list[str] = Field(default_factory=list)  # subagent roles
    context_strategy: str | None = None  # context-window strategy name
    # -- Tunable decode knobs (ADR 0017 / F-5) --------------------------------
    # These live HERE, on the Definition, as the single authoritative location.
    # They ENTER the content hash (what the Tuner searches, what state_dict
    # serializes) — but are hash-neutral when None so they don't change the sha
    # of any pre-existing frozen artifact. ``RunRequest.temperature`` is DERIVED
    # from the resolved spec via ``resolved_decode`` — never set independently.
    temperature: float | None = None
    top_p: float | None = None
    sample_k: int | None = None  # top-k sampling cutoff

    def decode_knobs(self) -> dict[str, float | int]:
        """The non-None tunable decode knobs as a plain dict (hash-stable ordering)."""
        out: dict[str, float | int] = {}
        for name in DECODE_KNOB_FIELDS:
            value = getattr(self, name)
            if value is not None:
                out[name] = value
        return out


class TeamSpec(BaseModel):
    agents: list[AgentSpec] = Field(default_factory=list)
    coordination: Coordination = Coordination.SINGLE
    lead: str | None = None  # coordinator role (for `lead` topology)
    workspace: Literal["shared", "isolated"] = "shared"
    # Which subset of the typed Context artifact to carry between agents
    # (full / recency / summary / typed_fields). None -> lossless default.
    context_carry: str | None = None


class Prompt(BaseModel):
    target: str
    text: str


class DefinitionRef(BaseModel):
    id: str
    version: str  # e.g. "0.2" or "0.1-sha"


class MCPConnection(BaseModel):
    """An MCP server connection authored in ``mcp/*.py``.

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
    """Export shape (stub — full hub package lands with the registry)."""

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

    def content_dict(self) -> dict[str, object]:
        """The canonical hash payload: the model dump minus the volatile ``version``,
        with each agent's decode knobs dropped when None.

        This is the migration seam for ADR 0017 / F-5. Adding the optional decode
        knobs to ``AgentSpec`` would otherwise emit ``"temperature": null`` (etc.)
        into every dump and perturb the sha of every pre-existing frozen artifact.
        Dropping them when None makes the new fields **hash-neutral for unmigrated
        artifacts** — their sha is byte-identical to the pre-v1 hash — while an
        artifact that pins any knob hashes differently (and must be re-frozen).
        """
        payload = self.model_dump(mode="json")
        # ``version`` is volatile (it CARRIES the sha) and ``id`` is identity, not
        # content (assigned per-instance by the canonical loader) — neither belongs
        # in the content hash.
        payload.pop("version", None)
        payload.pop("id", None)
        team = payload.get("team")
        if isinstance(team, dict):
            agents = team.get("agents")
            if isinstance(agents, list):
                for agent in agents:
                    if isinstance(agent, dict):
                        for name in DECODE_KNOB_FIELDS:
                            if agent.get(name) is None:
                                agent.pop(name, None)
        return payload

    def content_sha(self) -> str:
        """Deterministic 12-char content hash over :meth:`content_dict`.

        The single canonical hash function: structurally-identical Definitions
        collapse to one sha; any knob change diverges. Hash-neutral when no decode
        knob is set (see :meth:`content_dict`).
        """
        import hashlib
        import json

        blob = json.dumps(self.content_dict(), sort_keys=True, default=str).encode()
        return hashlib.sha256(blob).hexdigest()[:12]

    def resolved_decode(self, role: str | None = None) -> dict[str, float | int]:
        """The authoritative decode config for ``role`` (default: lead, else first).

        This is the ONE place a caller reads decode knobs at run time. The runtime
        derives ``RunRequest.temperature`` from this — it never sets its own value.
        Returns only the knobs the Definition actually pins (empty -> provider default).
        """
        spec: AgentSpec | None = None
        if role is not None:
            spec = self.agent(role)
        if spec is None:
            lead = self.team.lead
            if lead is not None:
                spec = self.agent(lead)
        if spec is None and self.team.agents:
            spec = self.team.agents[0]
        return spec.decode_knobs() if spec is not None else {}

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

    def mutable(self, store: Store, *, org_id: str = "local") -> AbstractContextManager[Borrow]:
        """Acquire an exclusive borrow on this Definition for training/mutation (F-7).

        Thin delegator to :func:`crawfish.borrow.mutable` — the documented follow-up
        that wires the borrow-lifetime semantics onto ``Definition`` without this module
        owning the borrow machinery::

            with defn.mutable(store) as draft:   # exclusive — train mode
                ...                               # no concurrent holder
            # released on exit (even on exception)

        Raises :class:`crawfish.borrow.ExclusiveBorrowError` if another holder already
        owns the borrow; every key is ``org_id``-scoped (tenancy isolation).
        """
        from crawfish.borrow import mutable as _mutable

        return _mutable(self, store, org_id=org_id)


# keep Version importable from here for the loader's convenience
_ = Version
