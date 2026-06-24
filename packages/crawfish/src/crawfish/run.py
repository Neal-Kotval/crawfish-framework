"""Run — one durable execution of a Definition against one input set.

A Run drives execution through the :class:`AgentRuntime` seam (the product model
never imports the SDK), binding fluid inputs as **session data** — never into the
instruction/system prompt (the boundary is enforced by the prompt compiler). It emits
OTel-shaped spans (run = trace, model/tool calls = spans) into the ``Store``, validates
every input slot before executing, idles durably on an approval gate without burning
compute, and is hard-killed at the cost cap (telemetry captured either way).
"""

from __future__ import annotations

import time
from enum import Enum

from crawfish.core.context import BudgetExceeded, RunContext
from crawfish.core.ids import new_id
from crawfish.core.types import JSONValue
from crawfish.definition.types import Definition
from crawfish.emission import Emission, EmissionKind, emit
from crawfish.grammar import Grammar
from crawfish.output import Output
from crawfish.retry import RetryPolicy, run_with_retry
from crawfish.runtime.base import AgentRuntime, EventKind, RunRequest, RunResult
from crawfish.runtime.prompt import pick_agent
from crawfish.runtime.team import run_team
from crawfish.store.base import Store
from crawfish.typesystem.registry import TypeRegistry, default_registry
from crawfish.validation import (
    ValidationAction,
    ValidationError,
    validate_inputs,
    validate_output,
)

__all__ = ["RunStatus", "Run", "InputBindingError", "InputValidationError", "RunSuspended"]


class RunStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SUSPENDED = "suspended"  # idling on an approval gate, state held durably


class InputBindingError(ValueError):
    """Raised when a required input slot is unbound before execution."""


class InputValidationError(ValueError):
    """Raised when a bound input value fails type validation (before any model call).

    Carries the structured :class:`~crawfish.validation.ValidationError` list so callers
    can route the failure through the configured :class:`ValidationAction`.
    """

    def __init__(self, errors: list[ValidationError]) -> None:
        self.errors = errors
        detail = "; ".join(f"{e.field}: {e.detail}" for e in errors)
        super().__init__(f"input validation failed: {detail}")


class OutputValidationError(RuntimeError):
    """Raised internally when a model output fails schema validation (drives REPAIR/DEAD_LETTER)."""

    def __init__(self, errors: list[ValidationError]) -> None:
        self.errors = errors
        detail = "; ".join(f"{e.field}: {e.detail}" for e in errors)
        super().__init__(f"output validation failed: {detail}")


class RunSuspended(RuntimeError):
    """Raised when a Run idles on an approval gate (state persisted, no compute spent)."""


class Run:
    """An agent team performing a single task."""

    def __init__(
        self,
        definition: Definition,
        inputs: dict[str, JSONValue] | None = None,
        *,
        runtime: AgentRuntime | None = None,
        requires_approval: bool = False,
        on_invalid: ValidationAction = ValidationAction.DEAD_LETTER,
        retry_policy: RetryPolicy | None = None,
        registry: TypeRegistry | None = None,
        validate_input_types: bool = True,
        validate_output_schema: bool = True,
        grammar: Grammar | None = None,
        decode_seed: int | None = None,
        id: str | None = None,
    ) -> None:
        self.id = id or new_id()
        self.definition = definition
        self.inputs: dict[str, JSONValue] = dict(inputs or {})
        self.runtime = runtime
        self.requires_approval = requires_approval
        # Validation action policy (CRA-172): how to handle an output that fails the
        # declared output schema. RETRY re-runs via RetryPolicy, REPAIR re-prompts the
        # model with the error (one extra metered call), DEAD_LETTER gives up.
        self.on_invalid = on_invalid
        self.retry_policy = retry_policy or RetryPolicy()
        self.registry: TypeRegistry = registry or default_registry
        # Some callers deliberately over-bind (e.g. the Router binds one item value to
        # every slot to satisfy presence) — they opt out of input *type* checking while
        # keeping the presence guard. Likewise a classifier ignores its declared output
        # schema (it reads free text), so it opts out of output validation.
        self.validate_input_types = validate_input_types
        self.validate_output_schema = validate_output_schema
        # Constrained / grammar-guided decoding (CRA-218 / TS-8). When ``grammar`` is set
        # the run takes the constrained path: the grammar is a static, author-supplied
        # (trusted) constraint serialized onto the per-call ``RunRequest.grammar`` field —
        # OUT of the Definition content hash (ADR 0017 / F-5). A grammar-honouring runtime
        # produces a well-formed structured field, so the validate+``_repair`` path becomes
        # dead code. ``grammar=None`` opts out (the "Let Me Speak Freely?" reasoning caveat)
        # even when the schema could yield one. ``decode_seed`` makes the constrained decode
        # reproducible (folded into the F-1 cassette key by the replay runtime).
        self.grammar = grammar
        self.decode_seed = decode_seed
        # Observability: how many metered repair re-prompts this run paid for. A
        # grammar-honouring constrained decode keeps this at 0 (the acceptance criterion).
        self.repair_count = 0
        self.status = RunStatus.PENDING
        self.output: Output[JSONValue] | None = None

    # -- validation ---------------------------------------------------------
    def validate(self) -> None:
        """Fail fast before any model call: required slots bound *and* typed.

        Presence is checked first (``InputBindingError`` for an unbound required slot),
        then each bound value's type is validated against its ``Parameter.type`` via the
        registry (``InputValidationError`` for a wrong-typed input).
        """
        provided = set(self.inputs)
        missing = [
            p.name
            for p in self.definition.inputs
            if p.required and p.default is None and p.name not in provided
        ]
        if missing:
            raise InputBindingError(f"missing required input(s): {missing}")

        if self.validate_input_types:
            errors = validate_inputs(self.inputs, self.definition.inputs, self.registry)
            if errors:
                raise InputValidationError(errors)

    # -- persistence / telemetry -------------------------------------------
    def _persist(self, ctx: RunContext) -> None:
        ctx.store.put_record(
            "run",
            self.id,
            {
                "id": self.id,
                "definition": self.definition.id,
                "version": str(self.definition.version),
                "status": self.status.value,
            },
            org_id=ctx.org_id,
        )

    def _emit_start(self, ctx: RunContext, runtime_name: str, **attrs: JSONValue) -> None:
        """Emit a typed RUN_START onto the ledger (replaces the loose run.start span)."""
        emit(
            ctx.store,
            Emission(
                kind=EmissionKind.RUN_START,
                run_id=ctx.run_id,
                org_id=ctx.org_id,
                node_id=self.id,
                attrs={"runtime": runtime_name, **attrs},
            ),
            org_id=ctx.org_id,
        )

    def _emit_finish(
        self, ctx: RunContext, status: str, *, tainted: bool = False, **attrs: JSONValue
    ) -> None:
        """Emit a typed RUN_FINISH onto the ledger (replaces the loose run.finish span).

        ``tainted`` propagates the producing ``Output.tainted`` marker across the
        emission boundary where the run handled an Output.
        """
        emit(
            ctx.store,
            Emission(
                kind=EmissionKind.RUN_FINISH,
                run_id=ctx.run_id,
                org_id=ctx.org_id,
                node_id=self.id,
                attrs={"status": status, **attrs},
                tainted=tainted,
            ),
            org_id=ctx.org_id,
        )

    # -- execution ----------------------------------------------------------
    async def execute(
        self,
        ctx: RunContext,
        runtime: AgentRuntime | None = None,
        *,
        approve: bool | None = None,
    ) -> Output[JSONValue]:
        """Execute the Definition's team on the bound inputs → a typed Output.

        ``approve`` gates a ``requires_approval`` Run: ``None`` (or ``False``) idles the
        run durably (``RunSuspended``) without spending compute; ``True`` proceeds.
        """
        rt = runtime or self.runtime
        if rt is None:
            raise ValueError("Run.execute requires an AgentRuntime")

        self.validate()

        # Approval gate: idle durably before burning any compute.
        if self.requires_approval and not approve:
            self.status = RunStatus.SUSPENDED
            self._persist(ctx)
            self._emit_finish(ctx, "suspended", reason="awaiting_approval")
            raise RunSuspended(f"run {self.id} awaiting approval")

        self.status = RunStatus.RUNNING
        self._persist(ctx)
        start = time.perf_counter()
        self._emit_start(ctx, rt.name, definition=self.definition.id)

        try:
            value, result = await self._produce_validated(ctx, rt)
        except BudgetExceeded as exc:
            self.status = RunStatus.FAILED
            self._persist(ctx)
            self._emit_finish(
                ctx,
                "failed",
                reason="budget_exceeded",
                detail=str(exc),
                latency_ms=(time.perf_counter() - start) * 1000,
            )
            raise
        except OutputValidationError as exc:
            # Validation exhausted its action policy (RETRY/REPAIR failed, or DEAD_LETTER).
            self.status = RunStatus.FAILED
            self._persist(ctx)
            self._emit_finish(
                ctx,
                "failed",
                reason="output_validation",
                detail=str(exc),
                latency_ms=(time.perf_counter() - start) * 1000,
            )
            raise
        except Exception as exc:
            self.status = RunStatus.FAILED
            self._persist(ctx)
            self._emit_finish(
                ctx,
                "failed",
                detail=str(exc),
                latency_ms=(time.perf_counter() - start) * 1000,
            )
            raise

        # Output schema derives from the Definition's declared outputs. The typed value
        # is fluid (untrusted model output): tainted if any input was fluid OR the run
        # consumed any tool/MCP result (a malicious tool output is an injection vector).
        from crawfish.runtime.prompt import split_inputs

        _static, fluid = split_inputs(self.definition, self.inputs)
        tool_tainted = any(e.kind is EventKind.TOOL_RESULT for e in result.events)
        out: Output[JSONValue] = Output(
            output_schema=list(self.definition.outputs),
            value=value,
            produced_by=self.id,
            tainted=bool(fluid) or tool_tainted,
        )
        out.persist(ctx.store, org_id=ctx.org_id)
        self.output = out
        self.status = RunStatus.DONE
        self._persist(ctx)
        # Taint propagates from the produced Output across the emission boundary.
        self._emit_finish(
            ctx,
            "done",
            tainted=out.tainted,
            cost_usd=result.cost_usd,
            latency_ms=(time.perf_counter() - start) * 1000,
        )
        return out

    # -- typed output production + validation policy ------------------------
    async def _produce_validated(
        self, ctx: RunContext, rt: AgentRuntime
    ) -> tuple[JSONValue, RunResult]:
        """Run the team and validate its output against the declared schema.

        Returns ``(typed_value, result)``. On a schema failure the configured
        :class:`ValidationAction` decides: RETRY re-runs the team (via ``RetryPolicy``),
        REPAIR re-prompts the model **once** with the error (a metered extra call that
        respects ``ctx.cost_budget``/``ctx.cancel_token``), DEAD_LETTER raises
        :class:`OutputValidationError`. The typed value is never silently coerced.
        """
        if self.on_invalid is ValidationAction.RETRY:
            return await run_with_retry(lambda: self._run_and_validate(ctx, rt), self.retry_policy)
        try:
            return await self._run_and_validate(ctx, rt)
        except OutputValidationError as exc:
            if self.on_invalid is ValidationAction.REPAIR:
                return await self._repair(ctx, rt, exc.errors)
            raise  # DEAD_LETTER

    async def _run_and_validate(
        self, ctx: RunContext, rt: AgentRuntime
    ) -> tuple[JSONValue, RunResult]:
        if self.grammar is not None:
            return await self._run_constrained(ctx, rt, self.grammar)
        result = await run_team(self.definition, self.inputs, ctx, rt)
        if not self.validate_output_schema:
            return result.text, result
        value, errors = validate_output(result.text, self.definition.outputs, self.registry)
        if errors:
            raise OutputValidationError(errors)
        return value, result

    async def _run_constrained(
        self, ctx: RunContext, rt: AgentRuntime, grammar: Grammar
    ) -> tuple[JSONValue, RunResult]:
        """The constrained-decode path: the runtime is told the output *shape* up front.

        The grammar (static, author-supplied) is serialized onto the per-call
        ``RunRequest.grammar`` field — never the Definition content hash — and the
        per-call ``decode_seed`` rides with it, so the decode is reproducible where the
        backend honours the seed (the F-1 cassette key folds it in). The grammar's
        deterministic, pure :meth:`~crawfish.grammar.Grammar.enforce` projects the result
        onto the constraint surface, so a malformed output is an *impossible* state — the
        validate+``_repair`` loop never runs (``repair_count`` stays 0). Where the backend
        cannot constrain, the projection still guarantees a well-formed field, so behaviour
        degrades gracefully to a single (un-repaired) call.
        """
        agent = pick_agent(self.definition, None)
        request = RunRequest(
            definition=self.definition,
            role=agent.role,
            inputs=self.inputs,
            grammar=grammar.to_request_grammar(),
            decode_seed=self.decode_seed,
        )
        result = await rt.run(request, ctx)
        # Runtime-mediated guarantee: snap the raw text onto the constraint surface.
        constrained_text = grammar.enforce(result.text)
        result = result.model_copy(update={"text": constrained_text})
        if not self.validate_output_schema:
            return constrained_text, result
        # The enforced field is well-formed by construction, so this validates without a
        # repair. We still run it so the typed value is parsed/canonicalised identically.
        value, errors = validate_output(constrained_text, self.definition.outputs, self.registry)
        if errors:
            raise OutputValidationError(errors)
        return value, result

    async def _repair(
        self, ctx: RunContext, rt: AgentRuntime, errors: list[ValidationError]
    ) -> tuple[JSONValue, RunResult]:
        """One repair re-prompt: re-run with the schema error fed back as fluid data.

        Cooperatively honours cancellation and refuses to spend past the ceiling: if the
        cost budget is already exhausted the repair is skipped (the original errors
        dead-letter) rather than spawning another metered model call. Otherwise the extra
        turn is charged through the runtime exactly like the first, so ``ctx.cost_budget``
        caps total spend and the overshoot is bounded by one call.
        """
        ctx.cancel_token.raise_if_cancelled()
        # Pre-flight: don't start a metered repair call with no headroom left. ``None``
        # means an unbounded (local-dev) budget; a real ceiling at/below zero dead-letters.
        remaining = ctx.cost_budget.remaining_usd
        if remaining is not None and remaining <= 0.0:
            raise OutputValidationError(errors)
        self.repair_count += 1
        detail = "; ".join(f"{e.field or '<root>'}: {e.detail}" for e in errors)
        repair_inputs = dict(self.inputs)
        # Fed as an ordinary (fluid) input — never concatenated into instructions.
        repair_inputs["_validation_error"] = (
            f"Your previous output failed schema validation: {detail}. "
            "Return only a corrected value that matches the declared output schema."
        )
        result = await run_team(self.definition, repair_inputs, ctx, rt)
        value, errors2 = validate_output(result.text, self.definition.outputs, self.registry)
        if errors2:
            raise OutputValidationError(errors2)
        return value, result

    # -- durability / recovery ---------------------------------------------
    @classmethod
    def restore(
        cls,
        store: Store,
        run_id: str,
        definition: Definition,
        *,
        runtime: AgentRuntime | None = None,
        org_id: str = "local",
    ) -> Run:
        """Rebuild a Run from its persisted record (restart recovery)."""
        record = store.get_record("run", run_id, org_id=org_id)
        if record is None:
            raise KeyError(f"no persisted run {run_id!r}")
        run = cls(definition, runtime=runtime, id=run_id)
        run.status = RunStatus(str(record["status"]))
        return run
