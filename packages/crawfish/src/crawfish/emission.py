"""The typed emission substrate — one signal everything emits.

Telemetry today is loose, untyped dicts written via ``Store.append_event`` and read
back via ``Store.events``. This module freezes the **contract** for a single typed
signal — :class:`Emission` — that every producer (runtime, tools, sinks, the tuner,
learning agents, the broker, the jail, observers, metrics) emits onto the append-only
ledger, and that every consumer (the dashboard #11, anomaly engine #14, inspector)
reads.

CRA-184 lands the *contract only*: the frozen model, the **closed**
:class:`EmissionKind` taxonomy, the required-``attrs`` schema per kind, and a
``schema_version`` so the ledger survives future kind/attr evolution. The behavioural
halves — routing the existing ``AgentRuntime._emit_telemetry`` through ``Emission``,
the ledger serialization, and the legacy-dict back-compat shim — land in CRA-171 and
the Store-migration work CRA-191; their entry points are stubs here.

Security spine: ``Emission.tainted`` propagates the fluid/untrusted marker across the
emission boundary. An emission carrying values derived from fluid input stays tainted,
so the dashboard and anomaly rules never treat untrusted content as trusted.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import Enum
from types import MappingProxyType

from pydantic import BaseModel, Field

from crawfish.core.ids import new_id
from crawfish.core.types import JSONValue

__all__ = [
    "EMISSION_SCHEMA_VERSION",
    "EmissionKind",
    "REQUIRED_ATTRS",
    "Emission",
]

# Bump when the Emission envelope or any kind's required-attrs change. The Store
# migration (CRA-191) keys off this; the dashboard/inspector read it to stay
# forward/backward compatible.
EMISSION_SCHEMA_VERSION = 1


class EmissionKind(str, Enum):
    """The **closed** taxonomy of signals. Adding a kind is a contract change
    (bump :data:`EMISSION_SCHEMA_VERSION` and extend :data:`REQUIRED_ATTRS`)."""

    RUN_START = "run_start"  # a pipeline/agent run began
    RUN_FINISH = "run_finish"  # a run completed (terminal)
    MODEL = "model"  # one model turn (cost/tokens/model id)
    TOOL = "tool"  # a tool/MCP call (result is untrusted -> tainted)
    SINK = "sink"  # a consequential side effect was attempted/committed
    COMPACTION = "compaction"  # context was compacted/summarized
    OBSERVER = "observer"  # an ObserverEvent crossed into the stream
    METRIC = "metric"  # a measured Metric/Rubric value
    SECRET_LEASE = "secret_lease"  # the broker leased a secret to a node (#8)
    JAIL_VIOLATION = "jail_violation"  # the sandbox blocked an escape attempt (#9)


# Required attribute keys per kind. The values carried in ``Emission.attrs`` must
# include at least these keys; consumers may rely on their presence. This is the
# canonical schema referenced by CRA-171/#8/#9/#11 and the taxonomy doc
# (docs/architecture/emission-taxonomy.md). Frozen to prevent accidental drift.
REQUIRED_ATTRS: Mapping[EmissionKind, tuple[str, ...]] = MappingProxyType(
    {
        EmissionKind.RUN_START: ("runtime",),
        EmissionKind.RUN_FINISH: ("status",),
        EmissionKind.MODEL: ("model", "cost_usd"),
        EmissionKind.TOOL: ("tool",),
        EmissionKind.SINK: ("target", "committed"),
        EmissionKind.COMPACTION: ("strategy",),
        EmissionKind.OBSERVER: ("kind", "severity"),
        EmissionKind.METRIC: ("metric", "value"),
        EmissionKind.SECRET_LEASE: ("ref", "node_id"),
        EmissionKind.JAIL_VIOLATION: ("attempt", "severity"),
    }
)


class Emission(BaseModel):
    """One typed signal on the append-only ledger. Frozen once created.

    ``attrs`` carries the kind-specific payload (see :data:`REQUIRED_ATTRS`).
    ``tainted`` propagates the fluid/untrusted marker across the emission boundary.
    """

    id: str = Field(default_factory=new_id)
    schema_version: int = EMISSION_SCHEMA_VERSION
    kind: EmissionKind
    run_id: str
    org_id: str = "local"  # tenancy key (CLAUDE.md: every Store row carries org_id)
    pipeline: str | None = None
    node_id: str | None = None  # agent/node that emitted, when applicable
    ts: float = 0.0  # epoch seconds; emitters stamp it, tests pass it for determinism
    attrs: dict[str, JSONValue] = Field(default_factory=dict)
    # Security: True when any value in ``attrs`` derives from fluid (untrusted) input.
    tainted: bool = False

    model_config = {"frozen": True}

    def missing_attrs(self) -> tuple[str, ...]:
        """Required-attr keys for this kind that are absent from ``attrs``.

        A pure contract check (no I/O): empty tuple means the emission satisfies
        its kind's schema. Used by CRA-171's emit path and the conformance suite.
        """
        required = REQUIRED_ATTRS.get(self.kind, ())
        return tuple(key for key in required if key not in self.attrs)

    def is_valid(self) -> bool:
        """True if ``attrs`` carries every key required for this kind."""
        return not self.missing_attrs()

    def to_event(self) -> dict[str, JSONValue]:
        """Serialize to a ledger event dict (implemented in CRA-171)."""
        raise NotImplementedError(
            "Emission.to_event is part of the ledger serialization landed in CRA-171"
        )

    @classmethod
    def from_event(cls, event: Mapping[str, JSONValue]) -> Emission:
        """Rehydrate from a (possibly legacy) ledger event dict, applying
        ``schema_version`` migration (implemented in CRA-171 / CRA-191)."""
        raise NotImplementedError(
            "Emission.from_event is the back-compat shim landed in CRA-171/CRA-191"
        )
