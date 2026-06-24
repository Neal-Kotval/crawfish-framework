"""The ``AgentRuntime`` seam.

The **only** place the model SDK/CLI is touched. The product model drives runs
through this interface, so the agent loop is swappable: CommandRuntime (`claude -p`,
zero key) → ClientRuntime (API key) → ManagedRuntime (CMA). Switching profile
dev→prod is a runtime swap, not a code change.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from enum import Enum
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from crawfish.core.ids import new_id
from crawfish.core.types import JSONValue
from crawfish.definition.types import Definition

if TYPE_CHECKING:
    from crawfish.core.context import RunContext

__all__ = [
    "EventKind",
    "DeterminismTier",
    "ToolCall",
    "RuntimeEvent",
    "RunRequest",
    "RunResult",
    "AgentRuntime",
]


class EventKind(str, Enum):
    TEXT = "text"
    TOOL_USE = "tool_use"
    TOOL_RESULT = "tool_result"
    RESULT = "result"
    ERROR = "error"


class DeterminismTier(str, Enum):
    """How faithfully a runtime backend honours a per-call ``decode_seed`` (ADR 0017).

    ``cw.calibrate()`` records the tier so model stochasticity is not conflated with
    infra-nondeterminism: a ``BEST_EFFORT``/``NONE`` backend has a variance floor
    attributed to infra, not to the Definition.
    """

    HONORS_SEED = "honors-seed"  # same seed + same inputs -> bit-identical decode
    BEST_EFFORT = "best-effort"  # seed nudges but does not guarantee reproduction
    NONE = "none"  # backend ignores the seed entirely (stochastic)


class ToolCall(BaseModel):
    id: str = Field(default_factory=new_id)
    name: str
    input: dict[str, JSONValue] = Field(default_factory=dict)


class RuntimeEvent(BaseModel):
    kind: EventKind
    text: str = ""
    tool: ToolCall | None = None
    cost_usd: float = 0.0
    session_id: str | None = None


class RunRequest(BaseModel):
    """One agent's turn: a compiled Definition + the inputs bound for this run.

    Decode-knob ownership (ADR 0017 / F-5):
      * The *tunable* knobs (``temperature``/``top_p``/``sample_k``) are owned by the
        Definition and ENTER its content hash. ``RunRequest`` does NOT carry its own
        independent temperature — :meth:`resolved_decode` DERIVES it from the resolved
        Definition. There is exactly one authoritative location.
      * ``grammar`` and ``decode_seed`` are *per-call* properties, kept OUT of the
        content hash. ``grammar`` is a provider dialect (degrades gracefully);
        ``decode_seed`` is folded into the F-1 replay cassette key, not the Definition.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    definition: Definition
    inputs: dict[str, JSONValue] = Field(default_factory=dict)
    role: str | None = None  # which agent (default: lead, else first)
    model: str | None = None  # per-agent/per-run override
    session_id: str | None = None  # resume an existing session
    # -- per-call decode properties (NOT in the Definition content hash) -------
    grammar: str | None = None  # provider-dialect constrained-decode grammar
    decode_seed: int | None = None  # per-call seed; F-1 folds it into the cassette key

    def resolved_decode(self) -> dict[str, float | int]:
        """The authoritative decode knobs for this turn, DERIVED from the Definition.

        Temperature/top_p/sample_k have exactly one source of truth — the resolved
        ``AgentSpec`` on the Definition. This is the only sanctioned way to read them
        on the request path; the request never holds an independent copy that could
        drift from (or conflict with) the content-hashed value.
        """
        return self.definition.resolved_decode(self.role)

    @property
    def temperature(self) -> float | None:
        """Derived temperature for this turn — read through the resolved Definition.

        Returns the Definition's temperature (the single authoritative source) or None
        when unpinned. Because this is a read-only derivation, no caller can set a
        conflicting independent value on the request.
        """
        value = self.resolved_decode().get("temperature")
        return float(value) if value is not None else None


class RunResult(BaseModel):
    text: str = ""
    session_id: str | None = None
    cost_usd: float = 0.0
    model: str = ""
    events: list[RuntimeEvent] = Field(default_factory=list)


# Definition is a concrete import (no cycle: crawfish.definition never imports runtime),
# so the RunRequest forward reference resolves at runtime.
RunRequest.model_rebuild()


class AgentRuntime(ABC):
    """Swappable agent-loop backend."""

    name: str = "abstract"
    # The determinism capability this backend advertises (ADR 0017). Default
    # BEST_EFFORT keeps every existing runtime valid without a code change; a backend
    # that bit-reproduces from a seed overrides this to HONORS_SEED, a fully stochastic
    # one to NONE. ``cw.calibrate()`` reads it to attribute a variance floor to infra.
    determinism_tier: DeterminismTier = DeterminismTier.BEST_EFFORT

    @abstractmethod
    async def run(self, request: RunRequest, ctx: RunContext) -> RunResult:
        """Execute one agent turn to completion and return the typed result."""

    async def stream(self, request: RunRequest, ctx: RunContext) -> AsyncIterator[RuntimeEvent]:
        """Stream events. Default: run to completion, then replay its events."""
        result = await self.run(request, ctx)
        for event in result.events:
            yield event

    @staticmethod
    def _emit_telemetry(ctx: RunContext, result: RunResult, runtime: str) -> None:
        """Persist a compact run summary to the Store's event ledger.

        Routed through the typed :class:`~crawfish.emission.Emission` substrate
        (``MODEL`` kind) and written via ``emit`` → ``Store.append_event``, so the
        transport and any ``ScrubbingStore`` redaction are unchanged. Pure runtime
        telemetry has no ``Output`` in hand, so ``tainted`` defaults False; ``ts`` is
        left at the contract default (no wall-clock read on this path).
        """
        # Local import keeps the runtime substrate free of an emission import cycle.
        from crawfish.emission import Emission, EmissionKind, emit

        emit(
            ctx.store,
            Emission(
                kind=EmissionKind.MODEL,
                run_id=ctx.run_id,
                org_id=ctx.org_id,
                attrs={
                    "model": result.model,
                    "cost_usd": result.cost_usd,
                    "events": len(result.events),
                    "session_id": result.session_id,
                    "runtime": runtime,
                },
            ),
            org_id=ctx.org_id,
        )
