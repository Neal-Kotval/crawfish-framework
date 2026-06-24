"""Workflow / Pipeline — the first-class deployable.

The top-level composition: ordered ``steps`` (Source / Filter / Batch / Aggregator /
Sink), Output threaded stage to stage, fan-out across steps. Adjacent steps are
**type-checked at assembly** (stage N's output schema ↔ stage N+1's inputs). Cross-node
orchestration state is checkpointed to the ``Store`` after each stage, so a crash
mid-workflow resumes from the last completed stage (durable by default).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

from crawfish.core.compat import parameters_compatible
from crawfish.core.context import RunContext
from crawfish.core.ids import new_id
from crawfish.core.types import JSONValue, Node, NodeKind, Parameter
from crawfish.definition.types import Definition
from crawfish.ledger import ExecutionLedger, compute_loop_id
from crawfish.nodes.aggregator import Aggregator
from crawfish.nodes.filter import Filter
from crawfish.nodes.router import Classifier, Router
from crawfish.nodes.sink import Sink
from crawfish.nodes.source import Source
from crawfish.output import Output, WireError, output_content_sha
from crawfish.run import Run
from crawfish.runtime.base import AgentRuntime

# Batch imported lazily inside methods to avoid a heavy import at module load.

__all__ = [
    "Workflow",
    "branch",
    "Program",
    "Edge",
    "UnboundedCycleError",
    "UnboundedRecursionError",
    "ProgramResult",
    "recurse",
    "Recurse",
    "RecurseResult",
]


def _branch_output_schema(node: Node) -> list[Parameter] | None:
    """The declared output schema a single branch node emits, or ``None`` if it is
    schema-transparent (Filter passthrough) / terminal (Sink)."""
    from crawfish.batch import Batch

    if isinstance(node, Batch):
        return list(node.definition.outputs)
    if isinstance(node, Aggregator):
        return list(node.output_schema)
    return None


def _router_output_schema(router: Router) -> list[Parameter] | None:
    """The shared downstream schema of a Router used as a producer.

    Every non-terminal branch must converge to a structurally-compatible output schema,
    else :class:`WireError` (you cannot wire a Router with divergent branch outputs into
    a later step). A Router whose every branch is a terminal :class:`Sink` is itself
    terminal and contributes no producer schema (``None``).
    """
    schemas: list[list[Parameter]] = []
    for br in router.branches.values():
        if isinstance(br, Sink):
            continue  # terminal branch contributes no downstream schema
        schema = _branch_output_schema(br)
        if schema is None:
            continue  # schema-transparent branch (Filter): no declared shape
        schemas.append(schema)
    if not schemas:
        return None
    first = schemas[0]
    first_by_name = {p.name: p for p in first}
    for other in schemas[1:]:
        other_by_name = {p.name: p for p in other}
        if set(first_by_name) != set(other_by_name):
            raise WireError(
                f"router {router.name!r}: branches emit divergent output fields "
                f"{sorted(first_by_name)} vs {sorted(other_by_name)} — a non-terminal "
                "Router must converge to a single output schema"
            )
        for name, want in first_by_name.items():
            if not parameters_compatible(other_by_name[name], want):
                raise WireError(
                    f"router {router.name!r}: branch output field {name!r} has "
                    f"incompatible types across branches"
                )
    return first


def branch(
    classifier: Classifier,
    branches: dict[str, Node],
    *,
    name: str = "router",
) -> Router:
    """Construct a runnable :class:`Router` composition step (C1).

    A thin, readable constructor: classify each item with ``classifier`` and dispatch it
    down the matching ``branches`` node. Totality is enforced at construction (an
    uncovered label raises :class:`~crawfish.nodes.router.UnroutableLabelError`); the
    Workflow's ``check_types`` then verifies every branch accepts the upstream output.
    """
    return Router(branches, classifier, name=name)


class Workflow:
    """A versioned pipeline of steps, run from a prompt and deployable as a unit."""

    def __init__(
        self,
        prompt: str = "",
        steps: list[Node] | None = None,
        *,
        name: str = "workflow",
        runtime: AgentRuntime | None = None,
        version: str = "0.1",
    ) -> None:
        self.id = new_id()
        self.prompt = prompt
        self.name = name
        self.steps: list[Node] = list(steps or [])
        self.runtime = runtime
        self.version = version

    # -- assembly type-check ------------------------------------------------
    def _producer_out(self, step: Node) -> list[Parameter] | None:
        from crawfish.batch import Batch

        if isinstance(step, Source):
            return step.outputs
        if isinstance(step, Batch):
            return step.definition.outputs
        if isinstance(step, Aggregator):
            return step.output_schema or None
        if isinstance(step, Router):
            # A Router's downstream schema is its branches' shared output schema — only
            # well-defined when every branch agrees (or the Router is terminal: all Sinks).
            return _router_output_schema(step)
        return None  # Filter passthrough / Sink terminal

    def _consumer_in(self, step: Node) -> list[Parameter] | None:
        from crawfish.batch import Batch

        if isinstance(step, Batch):
            return step.definition.inputs
        if isinstance(step, Router):
            # A Router consumes whatever EVERY branch can consume; the per-branch check in
            # ``_check_router`` enforces that each branch accepts the producer output.
            return None
        return None

    def _check_adjacency(self, a: Node, b: Node) -> None:
        """Reject a single type-incompatible producer→consumer edge at assembly."""
        out = self._producer_out(a)
        inp = self._consumer_in(b)
        if out is None or inp is None:
            return
        provided = {p.name: p for p in out}
        for want in inp:
            have = provided.get(want.name)
            if have is None:
                if want.required and want.default is None:
                    raise WireError(
                        f"workflow {self.name!r}: step {b.name!r} needs input "
                        f"{want.name!r} not produced by {a.name!r}"
                    )
                continue
            if not parameters_compatible(have, want):
                raise WireError(
                    f"workflow {self.name!r}: {a.name!r}->{b.name!r} type mismatch on "
                    f"{want.name!r} ({have.type!r} vs {want.type!r})"
                )

    def _check_router(self, producer: Node | None, router: Router) -> None:
        """Type-check a producer's output against EVERY branch's input (C1).

        Each branch must accept the producer output; a branch that cannot raises
        :class:`WireError` at assembly. (Branch-output consistency is validated lazily by
        ``_router_output_schema`` when the Router is itself a producer for a later step.)
        """
        out = self._producer_out(producer) if producer is not None else None
        if out is None:
            return
        for label, br in router.branches.items():
            inp = self._consumer_in(br)
            if inp is None:
                continue
            provided = {p.name: p for p in out}
            for want in inp:
                have = provided.get(want.name)
                if have is None:
                    if want.required and want.default is None:
                        raise WireError(
                            f"workflow {self.name!r}: router {router.name!r} branch "
                            f"{label!r} needs input {want.name!r} not produced upstream"
                        )
                    continue
                if not parameters_compatible(have, want):
                    raise WireError(
                        f"workflow {self.name!r}: router {router.name!r} branch {label!r} "
                        f"type mismatch on {want.name!r} ({have.type!r} vs {want.type!r})"
                    )

    def check_types(self) -> None:
        """Reject a type-incompatible adjacency at assembly."""
        for a, b in zip(self.steps, self.steps[1:], strict=False):
            if isinstance(b, Router):
                self._check_router(a, b)
            else:
                self._check_adjacency(a, b)
        # A trailing Router still type-checks its branches against its producer.
        if self.steps and isinstance(self.steps[0], Router):
            self._check_router(None, self.steps[0])

    # -- checkpoint state ---------------------------------------------------
    def _save_state(self, ctx: RunContext, current: list[Output[JSONValue]]) -> None:
        ctx.store.put_record(
            "workflow_state",
            self.id,
            {"current": [o.model_dump(mode="json") for o in current]},
            org_id=ctx.org_id,
        )

    def _load_state(self, ctx: RunContext) -> list[Output[JSONValue]]:
        rec = ctx.store.get_record("workflow_state", self.id, org_id=ctx.org_id)
        if rec is None:
            return []
        return [Output.model_validate(o) for o in rec["current"]]

    # -- execution ----------------------------------------------------------
    async def run(
        self,
        prompt: str | None = None,
        *,
        ctx: RunContext | None = None,
        runtime: AgentRuntime | None = None,
        resume: bool = False,
    ) -> list[Output[JSONValue]]:
        if prompt is not None:
            self.prompt = prompt
        rt = runtime or self.runtime
        if ctx is None:
            from crawfish.store.sqlite import SqliteStore

            ctx = RunContext(store=SqliteStore())
        self.check_types()

        ledger = ExecutionLedger(ctx.store, org_id=ctx.org_id)
        if resume:
            done = ledger.completed_steps(self.id)
            current = self._load_state(ctx)
        else:
            ledger.start_pipeline(self.id, self.version, total_items=len(self.steps))
            done = set()
            current = []

        for i, step in enumerate(self.steps):
            if i in done:
                continue
            ctx.cancel_token.raise_if_cancelled()
            current = await self._run_step(step, current, ctx, rt)
            ledger.checkpoint_step(self.id, i)
            self._save_state(ctx, current)

        ledger.finish_pipeline(self.id)
        return current

    async def _run_step(
        self,
        step: Node,
        current: list[Output[JSONValue]],
        ctx: RunContext,
        rt: AgentRuntime | None,
    ) -> list[Output[JSONValue]]:
        from crawfish.batch import Batch

        if isinstance(step, Source):
            out = await step.fetch(ctx)
            return step.fan_out(out) if step.multi else [out]

        if isinstance(step, Filter):
            # Filter the item Outputs directly so lineage + taint are preserved.
            return [o for o in current if step.predicate(o.value)]

        if isinstance(step, Batch):
            if rt is None:
                raise ValueError("workflow with a Batch step requires a runtime")
            outputs: list[Output[JSONValue]] = []
            for item in current:
                inputs = item.value if isinstance(item.value, dict) else {"item": item.value}
                child = RunContext(
                    store=ctx.store,
                    batch_id=step.id,
                    org_id=ctx.org_id,
                    cost_budget=ctx.cost_budget,
                )
                run = Run(step.definition, inputs, runtime=rt)
                out = await run.execute(child, rt)
                # Carry the source item's stable lineage forward for idempotency.
                outputs.append(out.model_copy(update={"lineage": item.lineage}))
            return outputs

        if isinstance(step, Aggregator):
            return [await step.reduce(current, ctx)]

        if isinstance(step, Sink):
            for item in current:
                await step.write(item, ctx)
            return current  # terminal: pass through unchanged

        if isinstance(step, Router):
            # C1: a Router is a runnable composition step. Each item is classified, then
            # dispatched through the SAME _run_step machinery as its chosen branch — so a
            # branch may be a Sink/Batch/Filter/Aggregator and inherits the identical
            # budget/taint/checkpoint guarantees (audit Gap #3). The classifier label is a
            # fluid-derived control signal that gates WHICH static branch fires; it never
            # itself becomes a consequential target (gap S3 invariant, enforced because the
            # branch set is closed + static at assembly).
            routed: list[Output[JSONValue]] = []
            for item in current:
                ctx.cancel_token.raise_if_cancelled()
                if rt is not None:
                    _label, br = await step.route_async(item, ctx, rt)
                else:
                    _label, br = step.route(item)
                branch_out = await self._run_step(br, [item], ctx, rt)
                # Taint/lineage carry across the branch boundary: the source item's stable
                # lineage is threaded forward so idempotency stays deterministic, and a
                # tainted item routed into a static-only Sink still raises inside that Sink.
                routed.extend(o.model_copy(update={"lineage": item.lineage}) for o in branch_out)
            return routed

        raise TypeError(f"unsupported workflow step kind: {step.kind}")


# ===========================================================================
# Program — the cyclic-capable composition surface (CRA-206 C2a / CRA-207 C2b).
# ===========================================================================


class UnboundedCycleError(ValueError):
    """Raised at assembly when a back-edge has no termination bound.

    A cycle that can iterate without a ``max_visits`` ceiling could loop forever; the
    ``Program`` driver bounds cycles by iteration / budget / cancel / no-progress —
    **never wall-clock** — so an unbounded back-edge is rejected before it can run.
    """


# A back-edge predicate: ``when(label, output) -> bool``. PURE (zero model calls) and
# STATIC by construction — it reads the frozen Output and the (optional) classifier label
# as data; it never derives a consequential target or an idempotency key from fluid input.
EdgeWhen = Callable[[str | None, Output[JSONValue]], bool]


@dataclass
class Edge:
    """A directed edge in a :class:`Program` graph; a *back*-edge may cycle.

    ``source``/``target`` are step indices. A back-edge (``target <= source``) re-enters
    the region ``[target .. source]`` while ``when`` holds, bounded by ``max_visits`` (a
    hard ceiling, assembly-required for a back-edge), a shared ``CostBudget``, cooperative
    cancel, and a calibrated no-progress detector. ``on_stuck`` names the terminal action
    when the bound trips without ``when`` going false.
    """

    source: int
    target: int
    when: EdgeWhen | None = None
    max_visits: int | None = None
    edge_id: str = field(default_factory=lambda: f"edge-{new_id()}")
    on_stuck: Literal["dead_letter", "return_last"] = "return_last"
    # F-8 calibrated no-progress band: a ranking delta within this band is treated as no
    # progress (replaces byte-identical-sha, which is too weak live / too aggressive on
    # replay). ``progress`` ranks an Output in [0, 1]; default is "no ranking" (0.0), so
    # the band never fires unless the author supplies a ranking function.
    progress: Callable[[Output[JSONValue]], float] | None = None
    rubric_std: float = 0.0
    no_progress_patience: int = 1

    @property
    def is_back_edge(self) -> bool:
        return self.target <= self.source


@dataclass(frozen=True)
class ProgramResult:
    """The typed outcome of one item's traversal through a :class:`Program`."""

    output: Output[JSONValue]
    visits: dict[str, int]  # edge_id -> number of back-edge traversals taken
    stopped: Literal["converged", "max_visits", "budget", "no_progress", "stuck"]


class Program(Workflow):
    """A typed directed graph whose edges may cycle (CRA-206 C2a).

    Reuses the :class:`Workflow` kernel (``_run_step``, ``check_types`` adjacency, the F-2
    ledger) — the difference is the *driver*: it walks edges per item rather than running
    ``for step in steps`` once. Every back-edge is a content-addressed version transition
    (``Output.derive`` mints a fresh sha; no in-place mutation) guarded by a deterministic
    predicate + bound. Cycles are bounded by iteration / shared budget / cancel /
    calibrated no-progress — never wall-clock.

    C2a is the spine (driver + assembly checks). Per-iteration ledger versioning + durable
    resume is layered on by C2b (``run(..., resume=True)`` over the F-2 composite-key
    ledger); recurse (C3) reuses this kernel with a depth bound.
    """

    def __init__(
        self,
        *,
        name: str = "program",
        runtime: AgentRuntime | None = None,
        version: str = "0.1",
    ) -> None:
        super().__init__(name=name, runtime=runtime, version=version)
        self.edges: list[Edge] = []
        # A back-edge may carry a classifier-style label forward to its ``when`` predicate
        # when its source step is a Router; otherwise the label is None.
        self._labels: dict[int, str | None] = {}

    # -- assembly -----------------------------------------------------------
    def step(self, node: Node) -> Node:
        """Register a step (a graph node) and return it for edge wiring."""
        self.steps.append(node)
        return node

    def edge(
        self,
        source: Node,
        target: Node,
        *,
        when: EdgeWhen | None = None,
        max_visits: int | None = None,
        on_stuck: Literal["dead_letter", "return_last"] = "return_last",
        progress: Callable[[Output[JSONValue]], float] | None = None,
        rubric_std: float = 0.0,
        no_progress_patience: int = 1,
        edge_id: str | None = None,
    ) -> Edge:
        """Wire a directed edge ``source -> target``; a back-edge (target earlier than
        source) may cycle and **requires** ``max_visits`` (else ``UnboundedCycleError``).
        """
        src_idx = self._index_of(source)
        dst_idx = self._index_of(target)
        e = Edge(
            source=src_idx,
            target=dst_idx,
            when=when,
            max_visits=max_visits,
            on_stuck=on_stuck,
            progress=progress,
            rubric_std=rubric_std,
            no_progress_patience=no_progress_patience,
            edge_id=edge_id or f"edge-{src_idx}-{dst_idx}",
        )
        self.edges.append(e)
        return e

    def _index_of(self, node: Node) -> int:
        for i, s in enumerate(self.steps):
            if s is node:
                return i
        raise ValueError(f"node {getattr(node, 'name', node)!r} is not a registered step")

    def check_types(self) -> None:
        """Validate a (possibly cyclic) graph: forward adjacency + every back-edge.

        Forward adjacency reuses the linear ``Workflow`` check. Each back-edge additionally
        requires its target to accept the source's output (structural compat, never string
        equality) and an explicit ``max_visits`` (unbounded ⇒ ``UnboundedCycleError``);
        reachability holds because every step sits on the forward chain.
        """
        super().check_types()
        for e in self.edges:
            if e.is_back_edge and e.max_visits is None:
                raise UnboundedCycleError(
                    f"program {self.name!r}: back-edge {e.edge_id!r} "
                    f"({self.steps[e.source].name!r} -> {self.steps[e.target].name!r}) "
                    "has no max_visits bound"
                )
            # The back-edge target must accept the source's produced output (structural).
            producer = self.steps[e.source]
            consumer = self.steps[e.target]
            if isinstance(consumer, Router):
                self._check_router(producer, consumer)
            else:
                self._check_adjacency(producer, consumer)

    # -- driver -------------------------------------------------------------
    async def run(
        self,
        prompt: str | None = None,
        *,
        ctx: RunContext | None = None,
        runtime: AgentRuntime | None = None,
        resume: bool = False,
    ) -> list[Output[JSONValue]]:
        """Run the program graph per item, walking forward and taking back-edges.

        The forward chain seeds items (Source/fan-out), then each item walks the cyclic
        region of any back-edge until ``when`` goes false or a bound trips. Spend meters
        into the one shared ``ctx.cost_budget``; cancel and the F-2 ledger checkpoint are
        honoured per iteration (durable resume is C2b).
        """
        if prompt is not None:
            self.prompt = prompt
        rt = runtime or self.runtime
        if ctx is None:
            from crawfish.store.sqlite import SqliteStore

            ctx = RunContext(store=SqliteStore())
        self.check_types()

        ledger = ExecutionLedger(ctx.store, org_id=ctx.org_id)
        if not resume:
            ledger.start_pipeline(self.id, self.version, total_items=len(self.steps))

        # Phase 1: run the forward prefix up to the first back-edge target to seed items.
        # For C2a the single-back-edge case covers branch-then-recurse + guarded loops;
        # multiple disjoint back-edges run their regions in source order.
        back_edges = sorted((e for e in self.edges if e.is_back_edge), key=lambda e: e.source)

        # Seed: run every step that is NOT inside a cyclic region, forward, once. The
        # cyclic region of a back-edge is [target .. source]; steps before the first
        # region run as a normal prefix.
        current: list[Output[JSONValue]] = []
        idx = 0
        n = len(self.steps)
        while idx < n:
            region = next((e for e in back_edges if e.target == idx), None)
            if region is None:
                ctx.cancel_token.raise_if_cancelled()
                current = await self._run_step(self.steps[idx], current, ctx, rt)
                idx += 1
                continue
            # Enter the cyclic region [region.target .. region.source]: drive each item
            # through it until convergence / bound.
            current = await self._drive_region(region, current, ctx, rt, ledger, resume=resume)
            idx = region.source + 1

        ledger.finish_pipeline(self.id)
        return current

    async def _drive_region(
        self,
        edge: Edge,
        items: list[Output[JSONValue]],
        ctx: RunContext,
        rt: AgentRuntime | None,
        ledger: ExecutionLedger,
        *,
        resume: bool,
    ) -> list[Output[JSONValue]]:
        """Drive every item through one back-edge's cyclic region to a fixed point/bound.

        Each item is processed independently (per-item edge walk). One pass runs steps
        ``[target .. source]``; if ``when`` still holds and the bound is not hit, a fresh
        content-addressed Output is derived and the next pass runs. Per pass: cancel check,
        budget preflight, F-2 ledger checkpoint of the derived ``output_content_sha``, and
        a calibrated no-progress test.
        """
        results: list[Output[JSONValue]] = []
        for seed in items:
            result = await self._drive_item(edge, seed, ctx, rt, ledger, resume=resume)
            results.append(result.output)
        return results

    async def _drive_item(
        self,
        edge: Edge,
        seed: Output[JSONValue],
        ctx: RunContext,
        rt: AgentRuntime | None,
        ledger: ExecutionLedger,
        *,
        resume: bool,
    ) -> ProgramResult:
        item_lineage = seed.lineage or seed.id
        item_id = item_lineage
        assert edge.max_visits is not None  # guaranteed by check_types
        # Deterministic loop identity (F-2): derived, never new_id(), so a second process
        # re-derives the same coordinate and resume re-charges $0 for committed visits.
        loop_id = compute_loop_id(self._region_version(edge), item_lineage, edge.edge_id)

        completed: set[int] = (
            ledger.completed_visits(loop_id, item_id, edge.edge_id) if resume else set()
        )

        current = seed
        last_label: str | None = None
        stale_streak = 0
        stopped: Literal["converged", "max_visits", "budget", "no_progress", "stuck"] = "converged"
        visits = 0

        for visit in range(edge.max_visits):
            ctx.cancel_token.raise_if_cancelled()
            replaying = visit in completed
            if not replaying:
                remaining = ctx.cost_budget.remaining_usd
                if remaining is not None and remaining <= 0.0:
                    stopped = "budget"
                    break

            prev = current
            # One pass through the region [target .. source]. The label (if the source is
            # a Router) is captured for the back-edge predicate.
            current, last_label = await self._run_region_pass(edge, current, ctx, rt)
            visits += 1

            # Mint a fresh content-addressed version for this back-edge traversal: derive a
            # new frozen Output (no in-place mutation) carrying unioned taint + lineage, and
            # record its content sha at the F-2 ledger coordinate (durable half is C2b).
            current = current.derive(
                value=current.value,
                produced_by=f"{self._region_version(edge)}#{edge.edge_id}#{visit}",
                tainted=bool(current.tainted or prev.tainted),
                lineage=item_lineage,
            )
            ledger.checkpoint_iteration(
                loop_id, item_id, edge.edge_id, visit, output_content_sha(current)
            )

            # Convergence: the back-edge predicate decides whether to keep looping. A None
            # predicate means "loop until the bound" (a pure counted loop).
            keep_looping = edge.when(last_label, current) if edge.when is not None else True
            if not keep_looping:
                stopped = "converged"
                break

            # Calibrated no-progress (F-8): a ranking delta within the band is noise.
            if edge.progress is not None and not replaying:
                delta = edge.progress(current) - edge.progress(prev)
                if delta <= edge.rubric_std:
                    stale_streak += 1
                else:
                    stale_streak = 0
                if stale_streak >= edge.no_progress_patience:
                    stopped = "no_progress"
                    break
        else:
            stopped = "max_visits"

        if stopped in ("max_visits", "no_progress", "budget") and edge.on_stuck == "dead_letter":
            from crawfish.retry import dead_letter

            dead_letter(
                ctx,
                batch_id=self.id,
                item_id=item_id,
                error=f"program back-edge {edge.edge_id!r} stuck ({stopped})",
                payload={"lineage": item_lineage},
            )
            stopped = "stuck"

        return ProgramResult(output=current, visits={edge.edge_id: visits}, stopped=stopped)

    async def _run_region_pass(
        self,
        edge: Edge,
        item: Output[JSONValue],
        ctx: RunContext,
        rt: AgentRuntime | None,
    ) -> tuple[Output[JSONValue], str | None]:
        """Run steps ``[target .. source]`` once for a single item, returning the produced
        Output and the classifier label if the source step is a Router (else ``None``)."""
        current = [item]
        label: str | None = None
        for i in range(edge.target, edge.source + 1):
            step = self.steps[i]
            if isinstance(step, Router) and i == edge.source:
                # Capture the routing label so the back-edge ``when`` can read it, then
                # dispatch the chosen branch through the shared kernel.
                if rt is not None:
                    label, br = await step.route_async(current[0], ctx, rt)
                else:
                    label, br = step.route(current[0])
                out = await self._run_step(br, current, ctx, rt)
                current = [o.model_copy(update={"lineage": current[0].lineage}) for o in out]
            else:
                current = await self._run_step(step, current, ctx, rt)
        # The region collapses to a single item Output (the loop body is per-item).
        return current[0], label

    def _region_version(self, edge: Edge) -> str:
        """A stable content version for a back-edge's cyclic region.

        Folds the content shas of the region's frozen Definitions (Batch bodies) so the
        loop_id is content-addressed: a change to any body mints a new coordinate. Falls
        back to the structural step ids when a step carries no Definition.
        """
        from crawfish.batch import Batch

        parts: list[str] = []
        for i in range(edge.target, edge.source + 1):
            step = self.steps[i]
            if isinstance(step, Batch):
                parts.append(step.definition.content_sha())
            else:
                parts.append(getattr(step, "name", str(i)))
        return "|".join(parts)


# ===========================================================================
# recurse — bounded self-referential Definition invocation (CRA-208 C3).
# ===========================================================================


class UnboundedRecursionError(ValueError):
    """Raised at assembly when :func:`recurse` is built without a ``max_depth`` bound.

    ``max_depth`` is the termination argument (distinct from a loop's ``max_visits``):
    a recursion with no depth ceiling could descend forever, so it is rejected before it
    can run. The whole-tree shared budget is the second guard against ``O(b^d)`` fan-out.
    """


# A pure base-case predicate: descent stops when it holds. It receives the frozen Output
# AND the **engine-authoritative** descent depth (the 0-based index of the level that just
# produced the Output) — a trusted, deterministic counter the engine owns. The predicate
# must never infer depth from the (stochastic, possibly marker-less) model Output: a body
# need not echo any depth marker, so a depth decision read from fluid output is unsound.
BaseCase = Callable[[Output[JSONValue], int], bool]
# A combine/fold over the (descent-order) child Outputs → one value. Existing reducers
# (``collect``/``count``/``dedupe``) satisfy this signature.
Combine = Callable[[list[Output[JSONValue]], RunContext], JSONValue]


@dataclass(frozen=True)
class RecurseResult:
    """The typed outcome of one item's bounded recursion."""

    output: Output[JSONValue]
    depth_reached: int
    stopped: Literal["base_case", "max_depth", "budget", "no_progress", "stuck"]


class Recurse(Node):
    """A depth-guarded back-edge re-entering the same FROZEN ``Definition`` (C3).

    Resolves the vision §5 open question: recursion is a :class:`Program` back-edge into
    the *same* Definition, pushing a frozen version onto a per-item depth stack. Reuses the
    C2 kernel; the only deltas are a **depth bound** (``max_depth``, assembly-required) and
    a pure **base-case predicate** ``base_case(output, depth) -> bool``. Each descent
    ``derive()``s a fresh content sha (no in-place mutation); the base case stops descent;
    ``combine`` folds the children in descent (depth-first) order. The reduced Output is
    **tainted if ANY child input was tainted** (taint = union; a vote/fold never launders
    taint).

    **Safety: depth is engine-authoritative.** ``base_case`` receives the trusted, 0-based
    descent ``depth`` the engine owns (the index of the level that just produced
    ``output``) — never a depth inferred from the stochastic model Output. A body need not
    echo any depth marker, so a "how deep am I / am I done" decision read from fluid output
    is unsound; termination decisions therefore run off trusted engine state.

    Halts on ``base_case`` / ``depth >= max_depth`` / budget / cancel / calibrated
    no-progress — never wall-clock. Each level checkpoints into the F-2 depth-variant
    ledger, so resume at depth *k* replays ``1..k-1`` at $0.
    """

    node_kind_tag = "recurse"

    def __init__(
        self,
        body: Definition,
        *,
        base_case: BaseCase,
        max_depth: int | None,
        combine: Combine,
        on_stuck: Literal["dead_letter", "return_last"] = "return_last",
        edge_id: str = "recurse",
        progress: Callable[[Output[JSONValue]], float] | None = None,
        rubric_std: float = 0.0,
        no_progress_patience: int = 1,
        name: str = "recurse",
    ) -> None:
        if max_depth is None:
            raise UnboundedRecursionError(
                f"recurse {name!r}: max_depth is required (an unbounded recursion is rejected)"
            )
        if max_depth < 1:
            raise ValueError("max_depth must be >= 1")
        self.id = new_id()
        self.name = name
        self.kind = NodeKind.AGGREGATOR  # reduce-shaped: a tree of attempts → one Output.
        self.body = body
        self.base_case = base_case
        self.max_depth = max_depth
        self.combine = combine
        self.on_stuck = on_stuck
        self.edge_id = edge_id
        self.progress = progress
        self.rubric_std = rubric_std
        self.no_progress_patience = no_progress_patience

    def _loop_id(self, item_lineage: str) -> str:
        """Deterministic recursion identity (F-2), content-addressed on the FROZEN body."""
        return compute_loop_id(self.body.content_sha(), item_lineage, self.edge_id)

    async def execute(
        self,
        seed: Output[JSONValue],
        ctx: RunContext,
        runtime: AgentRuntime,
        *,
        ledger: ExecutionLedger | None = None,
        resume: bool = False,
    ) -> RecurseResult:
        """Descend the frozen body on ``seed`` until the base case / a bound, then fold.

        Each level runs the body once, derives a fresh content-addressed Output (pushing
        the frozen version onto the per-item depth stack), and checkpoints it at the F-2
        depth coordinate. Descent stops at ``base_case(output, depth)`` — fed the engine-
        authoritative 0-based ``depth`` of the level just produced — / ``max_depth`` / budget
        / cancel / calibrated no-progress; ``combine`` then folds the descent-order children,
        unioning their taint onto the reduced Output.
        """
        item_lineage = seed.lineage or seed.id
        item_id = item_lineage
        loop_id = self._loop_id(item_lineage)
        completed: set[int] = (
            ledger.completed_depths(loop_id, item_id) if (ledger is not None and resume) else set()
        )

        children: list[Output[JSONValue]] = []
        current = seed
        stale_streak = 0
        stopped: Literal["base_case", "max_depth", "budget", "no_progress", "stuck"] = "max_depth"
        depth_reached = 0

        for depth in range(self.max_depth):
            ctx.cancel_token.raise_if_cancelled()
            replaying = depth in completed
            if not replaying:
                remaining = ctx.cost_budget.remaining_usd
                if remaining is not None and remaining <= 0.0:
                    stopped = "budget"
                    break

            prev = current
            produced = await self._run_level(current, depth, ctx, runtime)
            # Push the frozen version onto the depth stack: a fresh content-addressed
            # Output (no in-place mutation) carrying unioned taint + the item lineage.
            current = produced.derive(
                value=produced.value,
                produced_by=f"{self.body.content_sha()}#{self.edge_id}#d{depth}",
                tainted=bool(produced.tainted or prev.tainted),
                lineage=item_lineage,
            )
            children.append(current)
            depth_reached = depth + 1
            if ledger is not None:
                ledger.checkpoint_depth(loop_id, item_id, depth, output_content_sha(current))

            # Termination uses the engine-authoritative ``depth`` (0-based index of the
            # level that just produced ``current``), never a depth inferred from the
            # stochastic Output — a trusted-state decision, not a fluid-output one.
            if self.base_case(current, depth):
                stopped = "base_case"
                break

            if self.progress is not None and not replaying:
                delta = self.progress(current) - self.progress(prev)
                if delta <= self.rubric_std:
                    stale_streak += 1
                else:
                    stale_streak = 0
                if stale_streak >= self.no_progress_patience:
                    stopped = "no_progress"
                    break
        else:
            stopped = "max_depth"

        # Fold the descent-order children. Taint = union: the reduced Output is tainted if
        # ANY child was tainted ("a vote/fold does not launder taint").
        folded_value = self.combine(children, ctx)
        any_tainted = any(c.tainted for c in children) or seed.tainted
        folded = Output(
            value=folded_value,
            produced_by=f"{self.body.content_sha()}#{self.edge_id}#fold",
            tainted=any_tainted,
            lineage=item_lineage,
        )

        if stopped in ("max_depth", "no_progress", "budget") and self.on_stuck == "dead_letter":
            from crawfish.retry import dead_letter

            dead_letter(
                ctx,
                batch_id=self.id,
                item_id=item_id,
                error=f"recurse {self.edge_id!r} stuck ({stopped})",
                payload={"lineage": item_lineage},
            )
            stopped = "stuck"

        return RecurseResult(output=folded, depth_reached=depth_reached, stopped=stopped)

    async def _run_level(
        self, prior: Output[JSONValue], depth: int, ctx: RunContext, runtime: AgentRuntime
    ) -> Output[JSONValue]:
        """Run the frozen body once at ``depth``, feeding the prior level as FLUID data.

        The prior level's value rides in as ordinary (fluid) input — taint propagates, and
        the prompt compiler keeps it in the data block, never the instruction slot. The
        shared ``ctx`` is threaded so the one whole-tree budget meters this call.
        """
        inputs: dict[str, JSONValue] = {"_recurse_prior": prior.value}
        for param in self.body.inputs:
            if param.required and param.default is None and param.name != "_recurse_prior":
                inputs.setdefault(param.name, prior.value)
        run = Run(self.body, inputs, validate_input_types=False, validate_output_schema=False)
        return await run.execute(ctx, runtime)


def recurse(
    body: Definition,
    *,
    base_case: BaseCase,
    max_depth: int | None,
    combine: Combine,
    on_stuck: Literal["dead_letter", "return_last"] = "return_last",
    **kwargs: object,
) -> Recurse:
    """Construct a bounded, self-referential :class:`Recurse` over a frozen Definition.

    ``max_depth`` is mandatory (``None`` ⇒ :class:`UnboundedRecursionError` at construction
    / assembly); ``base_case(output, depth) -> bool`` is a pure predicate that stops descent,
    where ``depth`` is the **engine-authoritative** 0-based index of the level that produced
    ``output`` (never inferred from the stochastic Output); ``combine`` folds the descent-
    order children (an existing reducer like ``cw.collect`` works). The descent is whole-tree
    budget-bounded and content-addressed (each level mints a fresh sha).
    """
    return Recurse(
        body,
        base_case=base_case,
        max_depth=max_depth,
        combine=combine,
        on_stuck=on_stuck,
        **kwargs,  # type: ignore[arg-type]
    )
