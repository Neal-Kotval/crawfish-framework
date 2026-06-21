"""Crawfish — agents for bulk work over your data.

``Source → Batch (fan-out) → Aggregator (reduce) → Router (branch) → Sink``,
authored as directories and run locally via ``claude -p``.

This module re-exports the stable public surface. As primitives land (M1–M5) they
are added here, each placed in its stability tier.
"""

from __future__ import annotations

from crawfish.artifacts import (
    ArtifactRef,
    ArtifactStore,
    LocalArtifactStore,
    offload_if_large,
)
from crawfish.batch import Anomaly, Batch, Task
from crawfish.build import BuildPlan, generate_containerfile, plan_build, write_containerfile
from crawfish.ccexport import (
    ClaudeCodeAgent,
    ClaudeCodeSkill,
    definition_to_cc_agent,
    export_claude_code,
    map_tools,
    model_alias,
)
from crawfish.config import ProfileConfig, ProjectManifest, ProjectPaths, load_manifest
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
from crawfish.deploy import (
    DeployEntry,
    DeployRegistry,
    DeployStatus,
    Supervisor,
    deploy,
    stop,
)
from crawfish.discovery import Registry, UnitRef
from crawfish.doctor import DoctorFinding, DoctorReport, diagnose
from crawfish.emission import (
    EMISSION_SCHEMA_VERSION,
    REQUIRED_ATTRS,
    Emission,
    EmissionKind,
)
from crawfish.engine import Engine, run_pipeline
from crawfish.eval import (
    EvalCase,
    GoldenSet,
    LLMJudge,
    capture_case,
    gate_against_baseline,
    grade_output,
    load_baseline,
    save_baseline,
)
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
from crawfish.manage import PipelineStatus, format_table, manage_list, restart_target
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
from crawfish.observe import (
    ObserverEvent,
    ObserverSurface,
    RunInfo,
    Severity,
    parse_since,
)
from crawfish.observer import (
    CostSpike,
    FailureRateAbove,
    Observer,
    ObserverContext,
    Rule,
    StuckRun,
)
from crawfish.output import Output, WireError, check_wire, output_satisfies_inputs
from crawfish.provider import ModelsConfig, Provider, ProviderPolicy, resolve_model
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
from crawfish.sandbox import EgressBroker, EgressDenied, run_out_of_process
from crawfish.scaffold import scaffold_project
from crawfish.secrets import (
    Capabilities,
    Grant,
    ScrubbingStore,
    SecretManager,
    load_env,
    read_capabilities,
    redact,
    resolve_secret,
)
from crawfish.stability import (
    Stability,
    deprecated,
    experimental,
    is_breaking,
    stability_of,
    stable,
)
from crawfish.store import SqliteStore, Store
from crawfish.testing import (
    assert_rubric,
    assert_snapshot,
    replaying,
    run_fixtures,
    snapshot_match,
)
from crawfish.triggers import (
    Cron,
    CronSchedule,
    CronTrigger,
    Trigger,
    WebhookTrigger,
    verify_webhook,
)
from crawfish.typesystem import TypeDef, TypeKind, TypeRegistry, default_registry
from crawfish.validation import (
    StructuralDiff,
    ValidationError,
    ValidationFailure,
    structural_diff,
    validate_inputs,
    validate_output,
)
from crawfish.versioning import Freezable, FrozenError, Version
from crawfish.visualize import dashboard_state, serve_dashboard
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
    "ObserverEvent",
    "ObserverSurface",
    "RunInfo",
    "Severity",
    "parse_since",
    # operate / observe / integrate
    "DeployEntry",
    "DeployRegistry",
    "DeployStatus",
    "Supervisor",
    "deploy",
    "stop",
    "PipelineStatus",
    "manage_list",
    "format_table",
    "restart_target",
    "Observer",
    "ObserverContext",
    "Rule",
    "FailureRateAbove",
    "CostSpike",
    "StuckRun",
    "dashboard_state",
    "serve_dashboard",
    "ClaudeCodeAgent",
    "ClaudeCodeSkill",
    "definition_to_cc_agent",
    "export_claude_code",
    "map_tools",
    "model_alias",
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
    # eval data lifecycle (M4)
    "EvalCase",
    "GoldenSet",
    "LLMJudge",
    "capture_case",
    "grade_output",
    "save_baseline",
    "load_baseline",
    "gate_against_baseline",
    # authoring / packaging / ship (M5)
    "Registry",
    "UnitRef",
    "ProfileConfig",
    "ProjectManifest",
    "ProjectPaths",
    "load_manifest",
    "DoctorFinding",
    "DoctorReport",
    "diagnose",
    "Cron",
    "CronSchedule",
    "scaffold_project",
    "resolve_secret",
    "load_env",
    "SecretManager",
    "ScrubbingStore",
    "redact",
    "read_capabilities",
    "Capabilities",
    "snapshot_match",
    "assert_snapshot",
    "run_fixtures",
    "assert_rubric",
    "replaying",
    "generate_containerfile",
    "plan_build",
    "write_containerfile",
    "BuildPlan",
    "Trigger",
    "CronTrigger",
    "WebhookTrigger",
    "verify_webhook",
    "Stability",
    "stable",
    "experimental",
    "deprecated",
    "stability_of",
    "is_breaking",
    "EgressBroker",
    "EgressDenied",
    "run_out_of_process",
    # Phase 2 contracts (CRA-184 interface freeze)
    "Emission",
    "EmissionKind",
    "REQUIRED_ATTRS",
    "EMISSION_SCHEMA_VERSION",
    "ValidationFailure",
    "ValidationError",
    "StructuralDiff",
    "validate_output",
    "validate_inputs",
    "structural_diff",
    "Provider",
    "ProviderPolicy",
    "ModelsConfig",
    "resolve_model",
    "Grant",
]
