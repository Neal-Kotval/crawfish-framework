"""Definition — the directory-first agent-team package (CRA-102)."""

from __future__ import annotations

from crawfish.definition.compiler import DefinitionLoadError, load_definition
from crawfish.definition.types import (
    AgentSpec,
    Coordination,
    Definition,
    DefinitionAssets,
    DefinitionRef,
    MarketplacePackage,
    MCPConnection,
    Prompt,
    TeamSpec,
)

__all__ = [
    "AgentSpec",
    "TeamSpec",
    "Coordination",
    "Prompt",
    "DefinitionRef",
    "DefinitionAssets",
    "Definition",
    "MarketplacePackage",
    "MCPConnection",
    "load_definition",
    "DefinitionLoadError",
]
