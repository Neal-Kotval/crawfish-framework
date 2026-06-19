"""Crawfish — agents for bulk work over your data.

``Source → Batch (fan-out) → Aggregator (reduce) → Router (branch) → Sink``,
authored as directories and run locally via ``claude -p``.

This module re-exports the stable public surface. As primitives land (M1–M5) they
are added here; the stability tiers are governed by CRA-124.
"""

from __future__ import annotations

from crawfish.artifacts import (
    ArtifactRef,
    ArtifactStore,
    LocalArtifactStore,
    offload_if_large,
)
from crawfish.batch import Anomaly, Batch, Task
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
from crawfish.cost import (
    Budget,
    BudgetState,
    CostEstimate,
    CostMeter,
    estimate_cost,
    spent_today,
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
from crawfish.executor import (
    BatchExecutor,
    BatchRunResult,
    CycleError,
    DependencyGraph,
    ExecutionPlan,
    Roadmap,
)
from crawfish.inspector import RunReport, format_report, inspect_run, tail_events
from crawfish.ledger import ExecState, ExecutionLedger
from crawfish.memory import Memory
from crawfish.metrics import (
    Benchmark,
    Metric,
    Rubric,
    compare,
    confidence_threshold,
    field_present,
    is_nonempty,
    is_regression,
    output_number,
)
from crawfish.nodes import (
    Aggregator,
    ApprovalRequired,
    Classifier,
    Filter,
    GitHubPRSink,
    LinearSink,
    PullRequestSource,
    RepoSource,
    Router,
    Sink,
    Source,
    TargetMustBeStaticError,
    UnroutableLabelError,
    collect,
    concat,
    count,
    dedupe,
    definition_reducer,
    fan_in,
    fan_out,
    field_equals,
    field_matches,
    limit,
    title_contains,
)
from crawfish.output import Output, WireError, check_wire, output_satisfies_inputs
from crawfish.retry import ItemResult, ItemStatus, RetryPolicy
from crawfish.run import InputBindingError, Run, RunStatus, RunSuspended
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
from crawfish.workflow import Workflow

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
    # nodes (M2)
    "Source",
    "RepoSource",
    "PullRequestSource",
    "fan_out",
    "Sink",
    "LinearSink",
    "GitHubPRSink",
    "TargetMustBeStaticError",
    "ApprovalRequired",
    "Filter",
    "title_contains",
    "field_equals",
    "field_matches",
    "limit",
    "Memory",
    # run (M2)
    "Run",
    "RunStatus",
    "InputBindingError",
    "RunSuspended",
    # pipelines (M3)
    "Batch",
    "Task",
    "Anomaly",
    "Aggregator",
    "collect",
    "concat",
    "count",
    "dedupe",
    "definition_reducer",
    "fan_in",
    "Router",
    "Classifier",
    "UnroutableLabelError",
    "ArtifactRef",
    "ArtifactStore",
    "LocalArtifactStore",
    "offload_if_large",
    "DependencyGraph",
    "CycleError",
    "Roadmap",
    "ExecutionPlan",
    "BatchExecutor",
    "BatchRunResult",
    "ExecutionLedger",
    "ExecState",
    "RetryPolicy",
    "ItemResult",
    "ItemStatus",
    "Workflow",
    # measurement (M4)
    "Metric",
    "Rubric",
    "Benchmark",
    "output_number",
    "field_present",
    "is_nonempty",
    "confidence_threshold",
    "compare",
    "is_regression",
    "estimate_cost",
    "CostEstimate",
    "Budget",
    "BudgetState",
    "CostMeter",
    "spent_today",
    "inspect_run",
    "tail_events",
    "format_report",
    "RunReport",
]
