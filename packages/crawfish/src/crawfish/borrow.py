"""Borrow-lifetime / mode operational semantics (F-7 / CRA-200).

A ``Definition`` is normally a frozen, reproducible artifact. Mutating it (the
``train()`` / mode-switch path of the Agent Language) requires an **exclusive
borrow**: while one holder is mutating a definition, no other holder anywhere may
mutate the *same* definition concurrently.

The original spec called this "statically unaliasable". That overclaims — a borrow
that must hold across asynchronous tasks and separate processes cannot be proven
unaliasable by the type system alone. So this module implements the honest, weaker,
*enforceable* guarantee: a **dynamic exclusive borrow with an atomic acquire**, with
its lifetime fixed by a **context-manager protocol**:

    with mutable(defn, store) as m:   # acquires the borrow on enter
        ...                            # exclusive — no concurrent holder
    # released on exit (even on exception)

Enforcement is **Store-backed**, reusing the same atomic, tenancy-scoped,
race-safe primitive that consequential sinks use for idempotency
(``store.claim_idempotency``, see ``nodes/sink.py``) — *not* an in-process dict,
which would be invisible across processes and racy across async tasks.

Concurrency model — the *epoch + atomic claim* scheme
-----------------------------------------------------
``claim_idempotency(key)`` is single-shot: a key, once claimed, can never win
again. That is exactly the atomic gate we want for *one* acquisition, but a borrow
must be re-acquirable after release. So each definition carries a monotonically
increasing **epoch** in a ``borrow_lock`` record this module owns:

* **Acquire** reads the current epoch ``e`` from the ``borrow_lock`` record
  (absent ⇒ ``0``) and attempts ``claim_idempotency("borrow:<id>:acq:<e>")``.
  The atomic claim is the gate: if two tasks read the same ``e``, exactly one wins
  the claim. The winner writes ``{held: true, epoch: e}`` and holds the borrow.
  Any caller that loses the claim — or reads an epoch whose claim is already
  taken — raises :class:`ExclusiveBorrowError` deterministically.
* **Release** writes ``{held: false, epoch: e + 1}``. The next acquire reads the
  bumped epoch and claims a *fresh* ``acq:<e+1>`` key, so round-trips work without
  ever re-using (and thus without ever needing to delete) an idempotency claim.

Every key and every record is scoped by ``org_id``, so a borrow held by org ``"a"``
never blocks org ``"b"`` (tenancy isolation — a security property, not a nicety).

This module only ever touches the **public** ``Store`` API
(``get_record`` / ``put_record`` / ``claim_idempotency``); it owns the
``borrow_lock`` record namespace entirely and never edits store internals.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING, Protocol, cast, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Iterator

    from crawfish.core.types import JSONValue
    from crawfish.store.base import Store

__all__ = ["ExclusiveBorrowError", "Borrowable", "Borrow", "borrow_key", "mutable"]

#: The record namespace this module fully owns (a ``Store`` record ``kind``).
BORROW_RECORD_KIND = "borrow_lock"


class ExclusiveBorrowError(RuntimeError):
    """A mutable borrow was requested while another holder already owns it.

    Raised deterministically when an :func:`mutable` acquire loses the atomic
    claim for the current epoch — i.e. the same definition is already borrowed
    (possibly by another async task or another process) and not yet released.
    """


@runtime_checkable
class Borrowable(Protocol):
    """Anything with a stable identity can be borrowed.

    A structural protocol (not an import of ``Definition``) keeps this module
    decoupled from the definition package — ``Definition.id`` already satisfies
    it, and tests can borrow any object exposing a stable ``id``.
    """

    @property
    def id(self) -> str: ...


def borrow_key(target: Borrowable, epoch: int) -> str:
    """The deterministic, identity-derived idempotency key for one acquisition.

    Tenancy is applied by the ``Store`` via ``org_id`` (so the key itself stays
    org-agnostic and the same definition in two orgs maps to two distinct claims).
    """
    return f"borrow:{target.id}:acq:{epoch}"


def _record_id(target: Borrowable) -> str:
    return f"borrow:{target.id}"


def _current_epoch(store: Store, target: Borrowable, *, org_id: str) -> int:
    rec = store.get_record(BORROW_RECORD_KIND, _record_id(target), org_id=org_id)
    if rec is None:
        return 0
    return int(cast("int", rec.get("epoch", 0)))


class Borrow:
    """A live exclusive borrow handle yielded by :func:`mutable`.

    The handle is what ``with mutable(...) as m:`` binds. ``m.target`` is the
    borrowed object; release happens automatically when the ``with`` block exits.
    Calling :meth:`release` is idempotent (a double-release is a no-op) so the
    context manager's ``finally`` is always safe.
    """

    def __init__(self, target: Borrowable, store: Store, *, org_id: str, epoch: int) -> None:
        self.target = target
        self._store = store
        self._org_id = org_id
        self._epoch = epoch
        self._released = False

    @property
    def epoch(self) -> int:
        """The epoch this borrow was acquired at (for diagnostics/telemetry)."""
        return self._epoch

    @property
    def active(self) -> bool:
        return not self._released

    def release(self) -> None:
        """Release the borrow by bumping the epoch and marking it not-held.

        Idempotent: a second call is a no-op. The epoch bump means the next
        acquire claims a fresh, never-before-used idempotency key.
        """
        if self._released:
            return
        record: dict[str, JSONValue] = {"held": False, "epoch": self._epoch + 1}
        self._store.put_record(
            BORROW_RECORD_KIND, _record_id(self.target), record, org_id=self._org_id
        )
        self._released = True


def _acquire(target: Borrowable, store: Store, *, org_id: str) -> Borrow:
    epoch = _current_epoch(store, target, org_id=org_id)
    # The atomic gate. If another holder already won this epoch's claim, we lose
    # and refuse — deterministically, with no in-process state to race on.
    if not store.claim_idempotency(borrow_key(target, epoch), org_id=org_id):
        raise ExclusiveBorrowError(
            f"definition {target.id!r} is already exclusively borrowed "
            f"(epoch {epoch}, org {org_id!r}); a mutable borrow cannot span "
            f"concurrent holders"
        )
    held: dict[str, JSONValue] = {"held": True, "epoch": epoch}
    store.put_record(BORROW_RECORD_KIND, _record_id(target), held, org_id=org_id)
    return Borrow(target, store, org_id=org_id, epoch=epoch)


@contextmanager
def mutable(target: Borrowable, store: Store, *, org_id: str = "local") -> Iterator[Borrow]:
    """Acquire an exclusive borrow on ``target`` for the ``with`` block's lifetime.

    On enter: atomically claim the borrow; if another holder owns it, raise
    :class:`ExclusiveBorrowError`. On exit (normal or exceptional): release it.

    This is the ``defn.mutable()`` operational semantics. It is exposed as a free
    function (``mutable(defn, store)``) so it can be wired onto ``Definition`` later
    without this module owning that class; a thin ``Definition.mutable`` method can
    delegate straight to it.
    """
    handle = _acquire(target, store, org_id=org_id)
    try:
        yield handle
    finally:
        handle.release()
