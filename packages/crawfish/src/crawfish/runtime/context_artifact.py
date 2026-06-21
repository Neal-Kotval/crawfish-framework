"""Transferable typed Context artifact — first-class cross-agent state.

Before this, :mod:`crawfish.runtime.team` threaded one agent's result into the next
as a **raw string** (``{role}_result`` text stuffed into the next prompt). That is
lossy (typed values collapse to text), opaque (no lineage), and unsafe (no taint).

A :class:`Context` is the replacement: a **frozen, typed, taint-aware** artifact passed
between agents. Each :class:`ContextEntry` carries a typed value (not a string), its
declared schema, and — load-bearing — whether it is fluid/untrusted (``tainted``) plus
its ``lineage``. When an agent's result re-enters the next agent it arrives as **data**
(via the fluid-data block in :mod:`crawfish.runtime.prompt`), never as instructions, so
the static/fluid prompt-injection boundary holds (SECURITY.md).

Storable (ADR 0013): an entry's value is **inline by default**; large payloads opt in to
an :class:`~crawfish.artifacts.ArtifactRef`, dereferenced at a **single point**
(:meth:`Context.hydrate`). Persistence routes through the ``Store`` seam, so a
``ScrubbingStore`` redacts secrets — they are never embedded in the artifact.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from crawfish.artifacts.base import ArtifactRef
from crawfish.core.ids import new_id
from crawfish.core.types import JSONValue, Parameter
from crawfish.output import Output

if TYPE_CHECKING:
    from crawfish.artifacts.base import ArtifactStore
    from crawfish.store.base import Store

__all__ = [
    "ContextEntry",
    "Context",
    "ARTIFACT_THRESHOLD_BYTES",
]

# Payloads larger than this (serialized) opt into an ArtifactRef rather than inline
# value (ADR 0013: inline by default, ArtifactRef opt-in for large blobs).
ARTIFACT_THRESHOLD_BYTES = 32_768


class ContextEntry(BaseModel):
    """One typed value carried between agents. Frozen; taint + lineage propagate.

    ``value`` is the inline typed value (ADR 0013 default). When the value is large it
    is offloaded to an :class:`ArtifactRef` (``ref`` set, ``value`` left ``None``) and
    rehydrated at the single deref point :meth:`Context.hydrate`.
    """

    key: str  # e.g. "scout_result" — how the next agent addresses this value
    role: str  # the agent role that produced the value
    value: JSONValue = None
    value_schema: list[Parameter] = Field(default_factory=list)
    ref: ArtifactRef | None = None  # set iff offloaded to an ArtifactStore
    tainted: bool = False  # untrusted/fluid-derived (injection boundary)
    lineage: str | None = None

    model_config = {"frozen": True}

    @property
    def is_ref(self) -> bool:
        """True iff the value is offloaded to an ArtifactStore (needs hydration)."""
        return self.ref is not None


class Context(BaseModel):
    """The typed, taint-aware artifact threaded between agents. Frozen.

    Replaces raw-string threading: each :class:`ContextEntry` keeps its typed value,
    schema, taint and lineage. :meth:`add` returns a fresh Context (immutable
    derivation). :meth:`to_inputs` renders the carried entries as bound inputs for the
    next agent — tainted entries reach the model as fluid data, never instructions.
    """

    id: str = Field(default_factory=new_id)
    entries: list[ContextEntry] = Field(default_factory=list)

    model_config = {"frozen": True}

    # -- derivation ---------------------------------------------------------

    def add(self, entry: ContextEntry) -> Context:
        """Return a fresh Context with ``entry`` appended (immutable derivation)."""
        return self.model_copy(update={"entries": [*self.entries, entry]})

    def add_result(
        self,
        *,
        key: str,
        role: str,
        result: Output[JSONValue],
    ) -> Context:
        """Carry an agent's typed :class:`Output` forward as a Context entry.

        Taint and lineage propagate from the Output: a fluid-derived result stays
        tainted as it crosses into the next agent's context.
        """
        return self.add(
            ContextEntry(
                key=key,
                role=role,
                value=result.value,
                value_schema=list(result.output_schema),
                tainted=result.tainted,
                lineage=result.lineage,
            )
        )

    @property
    def tainted(self) -> bool:
        """True iff any carried entry is tainted (fluid-derived)."""
        return any(e.tainted for e in self.entries)

    def to_inputs(self) -> dict[str, JSONValue]:
        """Render carried entries as ``{key: value}`` inputs for the next agent.

        Refs must be hydrated first (:meth:`hydrate`); a still-offloaded entry yields
        its ref dict so callers never silently get ``None``.
        """
        out: dict[str, JSONValue] = {}
        for e in self.entries:
            out[e.key] = e.ref.model_dump(mode="json") if e.is_ref and e.ref else e.value
        return out

    # -- storage: ArtifactStore offload (ADR 0013) --------------------------

    def offload_large(
        self,
        store: ArtifactStore,
        *,
        org_id: str = "local",
        threshold: int = ARTIFACT_THRESHOLD_BYTES,
    ) -> Context:
        """Move oversized entry values into ``store``, replacing them with refs.

        Inline by default; an entry only offloads when its serialized value exceeds
        ``threshold`` (ADR 0013 opt-in). Taint/lineage/schema are preserved on the ref
        entry. Returns a fresh Context.
        """
        new_entries: list[ContextEntry] = []
        for e in self.entries:
            if e.is_ref or e.value is None:
                new_entries.append(e)
                continue
            data = json.dumps(e.value, sort_keys=True).encode("utf-8")
            if len(data) <= threshold:
                new_entries.append(e)
                continue
            ref = store.put(data, content_type="application/json", org_id=org_id)
            new_entries.append(e.model_copy(update={"value": None, "ref": ref}))
        return self.model_copy(update={"entries": new_entries})

    def hydrate(
        self,
        store: ArtifactStore,
        *,
        org_id: str = "local",
    ) -> Context:
        """The **single deref point** (ADR 0013): pull ref-backed values back inline.

        Reads each offloaded entry's bytes from the ArtifactStore exactly once and
        restores the inline typed value, preserving taint/lineage/schema.
        """
        new_entries: list[ContextEntry] = []
        for e in self.entries:
            if not e.is_ref or e.ref is None:
                new_entries.append(e)
                continue
            raw = store.get(e.ref, org_id=org_id)
            value: JSONValue = json.loads(raw.decode("utf-8"))
            new_entries.append(e.model_copy(update={"value": value, "ref": None}))
        return self.model_copy(update={"entries": new_entries})

    # -- storage: Store persistence (secrets scrubbed by the seam) ----------

    def persist(self, store: Store, *, org_id: str = "local") -> None:
        """Persist this Context through the ``Store`` seam (ScrubbingStore redacts)."""
        store.put_record("context", self.id, self.model_dump(mode="json"), org_id=org_id)

    @classmethod
    def load(cls, store: Store, context_id: str, *, org_id: str = "local") -> Context | None:
        """Load a persisted Context by id, or ``None`` if absent for this org."""
        data = store.get_record("context", context_id, org_id=org_id)
        if data is None:
            return None
        return cls.model_validate(data)
