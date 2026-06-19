"""Company Brain — the registry of Sources, Definitions & Outputs (CRA-111).

The Company Brain is the union of everything the system knows and can do: a
queryable read-model over every configured :class:`~crawfish.nodes.source.Source`,
authored/imported :class:`~crawfish.definition.types.Definition`, and produced
:class:`~crawfish.output.Output`. It is the substrate the shared hub / marketplace
export and (later phases) the data moat build on.

It is implemented as a **registry over the** :class:`~crawfish.store.base.Store`,
not an in-memory cache: every registration persists a serializable record through
the Store seam, so a fresh ``CompanyBrain`` over the same Store sees everything
that was registered before (persistence) and tenancy is enforced by the Store's
``org_id`` key. Output ``produced_by`` provenance survives the round-trip — that
provenance is the corpus later phases learn from.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from crawfish.core.types import JSONValue

if TYPE_CHECKING:
    from crawfish.definition.types import Definition
    from crawfish.nodes.source import Source
    from crawfish.output import Output
    from crawfish.store.base import Store

__all__ = ["CompanyBrain", "KIND_SOURCE", "KIND_DEFINITION", "KIND_OUTPUT"]

# Store record kinds — namespaced so brain rows never collide with raw Outputs
# (which the Output primitive persists under the bare ``"output"`` kind).
KIND_SOURCE = "brain_source"
KIND_DEFINITION = "brain_definition"
KIND_OUTPUT = "brain_output"


class CompanyBrain:
    """A Store-backed registry of Sources, Definitions, and Outputs.

    Register entities with :meth:`register_source` / :meth:`register_definition` /
    :meth:`register_output`; each call persists a serializable record. Look them up
    by id, version, capability, or producing node. Because the index lives in the
    Store, the Brain is per-``org_id`` and survives process restarts.
    """

    def __init__(self, store: Store, *, org_id: str = "local") -> None:
        self._store = store
        self._org_id = org_id

    # -- registration -------------------------------------------------------
    def register_source(self, source: Source[JSONValue]) -> None:
        """Persist a serializable record for a configured Source."""
        record: dict[str, JSONValue] = {
            "id": source.id,
            "name": source.name,
            "kind": source.kind.value,
            "multi": source.multi,
            # declared per-item output capability: the param names this source emits
            "outputs": [p.name for p in source.outputs],
        }
        self._store.put_record(KIND_SOURCE, source.id, record, org_id=self._org_id)

    def register_definition(self, definition: Definition) -> None:
        """Persist a serializable record for an authored/imported Definition."""
        record: dict[str, JSONValue] = {
            "id": definition.id,
            "version": str(definition.version),
            "frozen": definition.frozen,
            "inputs": [p.name for p in definition.inputs],
            "outputs": [p.name for p in definition.outputs],
        }
        # Key by id+version so version-pinned lookups can find the exact artifact
        # without colliding with other versions of the same Definition id.
        self._store.put_record(
            KIND_DEFINITION,
            self._def_key(definition.id, str(definition.version)),
            record,
            org_id=self._org_id,
        )

    def register_output(self, output: Output[JSONValue]) -> None:
        """Persist a serializable record for a produced Output (with provenance)."""
        record: dict[str, JSONValue] = {
            "id": output.id,
            "produced_by": output.produced_by,
            "output_schema": [p.name for p in output.output_schema],
        }
        self._store.put_record(KIND_OUTPUT, output.id, record, org_id=self._org_id)

    # -- listings -----------------------------------------------------------
    def list_sources(self) -> list[dict[str, JSONValue]]:
        """Every configured Source record under this org."""
        return self._store.list_records(KIND_SOURCE, org_id=self._org_id)

    def list_definitions(self) -> list[dict[str, JSONValue]]:
        """Every authored/imported Definition record under this org."""
        return self._store.list_records(KIND_DEFINITION, org_id=self._org_id)

    def list_outputs(self) -> list[dict[str, JSONValue]]:
        """Every produced Output record under this org."""
        return self._store.list_records(KIND_OUTPUT, org_id=self._org_id)

    # -- lookups ------------------------------------------------------------
    def definition(self, id: str, version: str | None = None) -> dict[str, JSONValue] | None:
        """Look up a Definition record by id, optionally pinned to a version.

        With ``version`` given, returns the exact frozen artifact record for that
        version (or ``None``). Without it, returns the first registered record for
        ``id`` (or ``None`` if none are known).
        """
        if version is not None:
            return self._store.get_record(
                KIND_DEFINITION, self._def_key(id, version), org_id=self._org_id
            )
        return next((d for d in self.list_definitions() if d.get("id") == id), None)

    def sources_by_capability(self, param_name: str) -> list[dict[str, JSONValue]]:
        """Sources whose declared outputs include ``param_name`` (a capability)."""
        return [s for s in self.list_sources() if param_name in (s.get("outputs") or [])]

    def outputs_by_producer(self, node_id: str) -> list[dict[str, JSONValue]]:
        """Outputs whose ``produced_by`` provenance points at ``node_id``."""
        return [o for o in self.list_outputs() if o.get("produced_by") == node_id]

    # -- internals ----------------------------------------------------------
    @staticmethod
    def _def_key(id: str, version: str) -> str:
        """The Store record id for a Definition: id pinned to a version."""
        return f"{id}@{version}"
