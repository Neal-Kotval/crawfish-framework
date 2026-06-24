"""Acceptance tests for F-7 / CRA-200 — exclusive borrow operational semantics.

These use a **real** :class:`SqliteStore` on disk (``tmp_path``), never a mock, so
the atomic-claim guarantee is exercised against the actual store primitive.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

import pytest

from crawfish.borrow import (
    BORROW_RECORD_KIND,
    Borrow,
    ExclusiveBorrowError,
    borrow_key,
    mutable,
)
from crawfish.store.sqlite import SqliteStore


@dataclass
class _Target:
    """A minimal :class:`crawfish.borrow.Borrowable` — just a stable identity."""

    id: str


@pytest.fixture
def store(tmp_path: Path) -> SqliteStore:
    s = SqliteStore(tmp_path / "borrow.db")
    yield s
    s.close()


def test_acquire_yields_active_handle(store: SqliteStore) -> None:
    target = _Target(id="d1")
    with mutable(target, store) as m:
        assert isinstance(m, Borrow)
        assert m.target is target
        assert m.active
        assert m.epoch == 0
    # released on exit
    assert not m.active


def test_concurrent_acquire_without_release_raises(store: SqliteStore) -> None:
    """Acceptance #1 (sequential form): a second acquire on the SAME object while
    the first is still held raises ``ExclusiveBorrowError`` deterministically."""
    target = _Target(id="d1")
    with mutable(target, store):
        with pytest.raises(ExclusiveBorrowError):
            with mutable(target, store):
                pass


def test_concurrent_acquire_across_async_tasks_raises(store: SqliteStore) -> None:
    """Acceptance #1 (async form): two ``mutable()`` acquires racing across two
    asyncio tasks — exactly one wins, the other raises ``ExclusiveBorrowError``."""
    target = _Target(id="d-async")
    outcomes: list[str] = []

    async def acquire(hold: asyncio.Event, release: asyncio.Event) -> None:
        try:
            with mutable(target, store):
                hold.set()  # signal: I hold the borrow
                outcomes.append("held")
                await release.wait()  # stay holding until told to drop
        except ExclusiveBorrowError:
            outcomes.append("denied")

    async def run() -> None:
        hold = asyncio.Event()
        release = asyncio.Event()
        # task A acquires and holds
        a = asyncio.create_task(acquire(hold, release))
        await hold.wait()
        # task B tries to acquire while A holds -> must be denied
        b = asyncio.create_task(acquire(asyncio.Event(), asyncio.Event()))
        await b
        release.set()
        await a

    asyncio.run(run())
    assert outcomes.count("held") == 1
    assert outcomes.count("denied") == 1


def test_sequential_acquire_release_reacquire(store: SqliteStore) -> None:
    """Acceptance #2: acquire, exit (release), re-acquire succeeds — and the epoch
    advances so a fresh idempotency claim is used each round."""
    target = _Target(id="d1")
    with mutable(target, store) as m1:
        assert m1.epoch == 0
    # re-acquire succeeds after release
    with mutable(target, store) as m2:
        assert m2.epoch == 1
    with mutable(target, store) as m3:
        assert m3.epoch == 2


def test_release_is_idempotent(store: SqliteStore) -> None:
    target = _Target(id="d1")
    with mutable(target, store) as m:
        m.release()
        assert not m.active
        m.release()  # no-op, must not raise
    # The context manager's own finally-release is also a no-op after the manual
    # releases. A subsequent acquire still works, and the epoch advanced exactly
    # once (a single live borrow -> a single epoch bump).
    with mutable(target, store) as m2:
        assert m2.epoch == 1


def test_cross_tenant_does_not_block(store: SqliteStore) -> None:
    """Acceptance #3: org 'a' holding a borrow does not block org 'b' on the same
    definition id — the borrow key is tenancy-scoped."""
    target = _Target(id="shared-def")
    with mutable(target, store, org_id="a"):
        # 'b' acquires the same definition id concurrently — must succeed
        with mutable(target, store, org_id="b") as m_b:
            assert m_b.active
        # and 'a' is still exclusively held against another 'a' acquire
        with pytest.raises(ExclusiveBorrowError):
            with mutable(target, store, org_id="a"):
                pass


def test_distinct_definitions_do_not_block(store: SqliteStore) -> None:
    with mutable(_Target(id="d1"), store):
        with mutable(_Target(id="d2"), store) as m2:
            assert m2.active


def test_release_on_exception(store: SqliteStore) -> None:
    """The context manager releases even when the body raises."""
    target = _Target(id="d1")
    with pytest.raises(ValueError):
        with mutable(target, store):
            raise ValueError("boom")
    # borrow was released despite the exception -> re-acquire succeeds
    with mutable(target, store) as m:
        assert m.active


def test_borrow_key_is_deterministic_and_identity_scoped() -> None:
    t = _Target(id="abc")
    assert borrow_key(t, 0) == "borrow:abc:acq:0"
    assert borrow_key(t, 3) == "borrow:abc:acq:3"
    assert borrow_key(_Target(id="xyz"), 0) == "borrow:xyz:acq:0"


def test_record_namespace_tracks_held_state(store: SqliteStore) -> None:
    target = _Target(id="d1")
    with mutable(target, store):
        rec = store.get_record(BORROW_RECORD_KIND, "borrow:d1", org_id="local")
        assert rec == {"held": True, "epoch": 0}
    rec = store.get_record(BORROW_RECORD_KIND, "borrow:d1", org_id="local")
    assert rec == {"held": False, "epoch": 1}
