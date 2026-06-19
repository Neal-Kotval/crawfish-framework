"""Crawfish — agents for bulk work over your data.

``Source → Batch (fan-out) → Aggregator (reduce) → Router (branch) → Sink``,
authored as directories and run locally via ``claude -p``.

This module re-exports the stable public surface. As primitives land (M1–M5) they
are added here; the stability tiers are governed by CRA-124.
"""

from __future__ import annotations

from crawfish.core import (
    BudgetExceeded,
    Cancelled,
    CancelToken,
    CostBudget,
    Flow,
    JSONValue,
    Node,
    NodeKind,
    Parameter,
    Policy,
    PolicyKind,
    RunContext,
    new_id,
    parameters_compatible,
)
from crawfish.definition import (
    AgentSpec,
    Coordination,
    Definition,
    DefinitionAssets,
    DefinitionLoadError,
    DefinitionRef,
    MarketplacePackage,
    MCPConnection,
    Prompt,
    TeamSpec,
    load_definition,
)
from crawfish.engine import Engine, run_pipeline
from crawfish.output import Output, WireError, check_wire, output_satisfies_inputs
from crawfish.runtime import (
    AgentRuntime,
    ClientRuntime,
    CommandRuntime,
    ManagedRuntime,
    MockRuntime,
    RecordReplayRuntime,
    RunRequest,
    RunResult,
    RuntimeEvent,
    get_runtime,
)
from crawfish.store import SqliteStore, Store
from crawfish.typesystem import TypeDef, TypeKind, TypeRegistry, default_registry
from crawfish.versioning import Freezable, FrozenError, Version

__version__ = "0.1.0"

__all__ = [
    "__version__",
    # core
    "JSONValue",
    "new_id",
    "Flow",
    "Parameter",
    "NodeKind",
    "Node",
    "PolicyKind",
    "Policy",
    "parameters_compatible",
    "RunContext",
    "CostBudget",
    "CancelToken",
    "BudgetExceeded",
    "Cancelled",
    # type system
    "TypeDef",
    "TypeKind",
    "TypeRegistry",
    "default_registry",
    # versioning
    "Version",
    "FrozenError",
    "Freezable",
    # store
    "Store",
    "SqliteStore",
    # engine
    "Engine",
    "run_pipeline",
    # output
    "Output",
    "output_satisfies_inputs",
    "check_wire",
    "WireError",
    # definition
    "Definition",
    "AgentSpec",
    "TeamSpec",
    "Coordination",
    "Prompt",
    "DefinitionRef",
    "DefinitionAssets",
    "MarketplacePackage",
    "MCPConnection",
    "load_definition",
    "DefinitionLoadError",
    # runtime
    "AgentRuntime",
    "CommandRuntime",
    "MockRuntime",
    "ClientRuntime",
    "ManagedRuntime",
    "RecordReplayRuntime",
    "RunRequest",
    "RunResult",
    "RuntimeEvent",
    "get_runtime",
]
