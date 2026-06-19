"""CRA-123: Store-backed Memory — working memory, cross-run dedup, atomic claim."""

from __future__ import annotations

from crawfish.core.context import RunContext
from crawfish.memory import Memory
from crawfish.store import SqliteStore


def test_set_get_round_trip_scoped_by_namespace() -> None:
    store = SqliteStore()
    a = Memory(store, "ns-a")
    b = Memory(store, "ns-b")

    a.set("k", {"hello": "world"})
    assert a.get("k") == {"hello": "world"}
    assert a.get("missing") is None
    # Different namespaces don't collide.
    assert b.get("k") is None


def test_dedup_persists_across_runs() -> None:
    store = SqliteStore()

    first = Memory(store, "tickets")
    assert first.already_processed("ticket-1") is False
    first.mark_processed("ticket-1")
    assert first.already_processed("ticket-1") is True

    # A second Memory over the SAME store simulates a fresh run; state survives.
    second = Memory(store, "tickets")
    assert second.already_processed("ticket-1") is True
    assert second.already_processed("ticket-2") is False


def test_claim_wins_exactly_once() -> None:
    store = SqliteStore()
    mem = Memory(store, "jobs")

    assert mem.claim("x") is True
    assert mem.claim("x") is False
    # A different id is still claimable.
    assert mem.claim("y") is True


def test_for_run_builds_from_run_context() -> None:
    store = SqliteStore()
    ctx = RunContext(store=store, org_id="acme")
    mem = Memory.for_run(ctx, "ns")

    mem.set("k", 42)
    assert mem.get("k") == 42

    # Built with ctx.org_id, so it shares state with an explicitly-scoped Memory.
    scoped = Memory(store, "ns", org_id="acme")
    assert scoped.get("k") == 42
    # ...and is isolated from the default org.
    assert Memory(store, "ns").get("k") is None
