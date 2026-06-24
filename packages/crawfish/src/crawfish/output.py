"""Output — the typed, self-describing envelope between nodes.

An ``Output`` carries a value, the schema of that value, and the id of the node
that produced it. It is **immutable once produced** (frozen): Filters and other
transforms derive a *fresh* Output via :meth:`derive`, keeping the upstream value
intact for audit. Wiring two nodes is allowed only when an upstream Output's schema
satisfies the downstream node's required inputs (structural check).
"""

from __future__ import annotations

import hashlib
import json
from typing import Generic

from pydantic import BaseModel, Field

from crawfish.core.compat import parameters_compatible
from crawfish.core.ids import new_id
from crawfish.core.types import JSONValue, Parameter, T
from crawfish.typesystem.registry import TypeRegistry

__all__ = [
    "Output",
    "output_satisfies_inputs",
    "check_wire",
    "WireError",
    "output_content_sha",
]


class WireError(TypeError):
    """Raised when an upstream Output cannot wire into a downstream node's inputs."""


class Output(BaseModel, Generic[T]):
    """The unit of data flowing between nodes. Frozen once produced."""

    id: str = Field(default_factory=new_id)
    output_schema: list[Parameter] = Field(default_factory=list)  # shape of `value`
    value: T
    produced_by: str  # node id that emitted it
    # Stable per-item lineage (the source item's identity), threaded through the
    # pipeline so idempotency keys are deterministic across re-runs.
    # Distinct from `id` (a fresh UUID per Output instance).
    lineage: str | None = None
    # Taint: True when this value derives from fluid (untrusted) input. A tainted
    # value must never become a Sink target or an idempotency key.
    # Propagates through `derive`.
    tainted: bool = False

    model_config = {"frozen": True}

    def derive(
        self,
        *,
        value: JSONValue,
        produced_by: str,
        output_schema: list[Parameter] | None = None,
        tainted: bool | None = None,
        lineage: str | None = None,
    ) -> Output[JSONValue]:
        """Create a fresh Output from this one (the immutable-derivation path).

        Taint and lineage propagate: a value derived from a tainted Output stays
        tainted, and keeps the upstream lineage, unless explicitly overridden.
        """
        return Output(
            value=value,
            produced_by=produced_by,
            output_schema=output_schema if output_schema is not None else list(self.output_schema),
            tainted=self.tainted if tainted is None else tainted,
            lineage=self.lineage if lineage is None else lineage,
        )

    def persist(self, store: object, *, org_id: str = "local") -> None:
        """Persist this Output through the ``Store`` seam."""
        # Imported lazily / typed loosely to avoid a hard import cycle with store.
        store.put_record(  # type: ignore[attr-defined]
            "output", self.id, self.model_dump(mode="json"), org_id=org_id
        )


def output_satisfies_inputs(
    output: Output[object],
    inputs: list[Parameter],
    *,
    registry: TypeRegistry | None = None,
) -> bool:
    """True if ``output``'s schema can satisfy every *required* downstream input.

    Each required input must be matched by name to a parameter in the output's
    schema whose type is structurally compatible (producer → consumer).
    """
    by_name = {p.name: p for p in output.output_schema}
    for want in inputs:
        have = by_name.get(want.name)
        if have is None:
            if want.required and want.default is None:
                return False
            continue
        if not parameters_compatible(have, want, registry):
            return False
    return True


def check_wire(
    output: Output[object],
    inputs: list[Parameter],
    *,
    registry: TypeRegistry | None = None,
) -> None:
    """Raise :class:`WireError` if ``output`` cannot wire into ``inputs``."""
    if not output_satisfies_inputs(output, inputs, registry=registry):
        names = {p.name for p in output.output_schema}
        wanted = {p.name: p.type for p in inputs}
        raise WireError(f"output (schema fields {sorted(names)}) cannot satisfy inputs {wanted}")


# Fields that define an Output's *content* identity. The volatile per-instance
# ``id`` is excluded so that two structurally-equal Outputs (same value/schema/
# producer/lineage/taint) hash equal even though each carries a fresh UUID. Bump
# ``_CONTENT_SHA_VERSION`` if this field set ever changes (it changes the digest).
_CONTENT_SHA_FIELDS = ("output_schema", "value", "produced_by", "lineage", "tainted")
_CONTENT_SHA_VERSION = 1


def output_content_sha(o: Output[object]) -> str:
    """Return a stable hex SHA-256 digest of an ``Output``'s content.

    Pure function over a frozen value: no mutation, no model call, no I/O. The
    digest is computed over canonical JSON (``sort_keys=True`` with tight
    separators) of the content-defining fields only — the volatile ``id`` is
    excluded. Consequently two structurally-equal Outputs hash equal even when
    their ``id`` differs, and the digest is identical across processes and runs.
    """
    dumped = o.model_dump(mode="json")
    content = {field: dumped[field] for field in _CONTENT_SHA_FIELDS}
    payload = {"v": _CONTENT_SHA_VERSION, "content": content}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
