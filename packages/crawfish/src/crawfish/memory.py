"""Memory / state primitive — cross-run dedup + working memory (CRA-123).

A thin, ``Store``-backed key/value handle scoped to a ``(namespace, org_id)``
pair. It gives a pipeline three things on top of the persistence seam:

* **Working memory** — ``get``/``set`` round-trip JSON values, isolated per
  namespace so unrelated stages never collide.
* **Cross-run dedup** — ``already_processed``/``mark_processed`` remember which
  item ids a pipeline has handled, surviving across runs because the state lives
  in the ``Store``, not in process memory.
* **Atomic claim** — ``claim`` wins exactly once per item id (delegating to the
  store's ``claim_idempotency``), so ``if mem.claim(id): process(id)`` is safe
  even under concurrency.

The ``for_run`` classmethod is the "exposed via :class:`RunContext`" path: it
builds a :class:`Memory` from ``ctx.store`` and ``ctx.org_id``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from crawfish.core.types import JSONValue

if TYPE_CHECKING:
    from crawfish.core.context import RunContext
    from crawfish.store.base import Store

__all__ = ["Memory"]


class Memory:
    """A ``Store``-backed KV/dedup handle scoped to ``(namespace, org_id)``."""

    def __init__(self, store: Store, namespace: str, *, org_id: str = "local") -> None:
        self._store = store
        self._namespace = namespace
        self._org_id = org_id

    @classmethod
    def for_run(cls, ctx: RunContext, namespace: str) -> Memory:
        """Build a :class:`Memory` from a :class:`RunContext` (store + org_id)."""
        return cls(ctx.store, namespace, org_id=ctx.org_id)

    # -- working memory -----------------------------------------------------
    def get(self, key: str) -> JSONValue | None:
        """Return the value stored at ``key`` in this namespace, or ``None``."""
        return self._store.kv_get(self._namespace, key, org_id=self._org_id)

    def set(self, key: str, value: JSONValue) -> None:
        """Store ``value`` at ``key`` within this namespace."""
        self._store.kv_set(self._namespace, key, value, org_id=self._org_id)

    # -- cross-run dedup ----------------------------------------------------
    def already_processed(self, item_id: str) -> bool:
        """True iff ``item_id`` was previously marked via :meth:`mark_processed`."""
        return self.get(self._seen_key(item_id)) is True

    def mark_processed(self, item_id: str) -> None:
        """Record ``item_id`` as processed (persists across runs)."""
        self.set(self._seen_key(item_id), True)

    def claim(self, item_id: str) -> bool:
        """Atomically claim ``item_id``.

        Returns ``True`` the first time an id is seen and ``False`` thereafter,
        so a pipeline can guard work with ``if mem.claim(id): process(id)``.
        Backed by the store's idempotency table, so the claim is durable and
        safe under concurrency.
        """
        return self._store.claim_idempotency(self._claim_key(item_id), org_id=self._org_id)

    # -- key helpers --------------------------------------------------------
    def _seen_key(self, item_id: str) -> str:
        return f"seen:{item_id}"

    def _claim_key(self, item_id: str) -> str:
        # Namespace the idempotency key so claims in different memories/stages
        # don't shadow one another in the shared idempotency table.
        return f"{self._namespace}:claim:{item_id}"
