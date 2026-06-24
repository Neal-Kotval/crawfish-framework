"""F-2 / CRA-195 — loop/recurse iteration ledger (composite key).

Acceptance criteria, exercised over a real on-disk ``SqliteStore`` in ``tmp_path``:

1. determinism: two process-like ``ExecutionLedger`` instances over the same store
   produce the same ``loop_id`` for the same loop over the same item.
2. resume re-charges $0: completed visits load, not re-run.
3. cross-``org_id`` isolation: org "b" sees none of org "a"'s iterations.
4. ``loop_id`` is a pure function of inputs (no ``new_id()``): call twice ⇒ equal;
   change ``edge_id`` ⇒ different.
5. the depth variant round-trips for ``recurse``.
6. no regression: the existing ``ledger_pipeline`` ``checkpoint_step`` still works.
"""

from __future__ import annotations

from pathlib import Path

from crawfish.ledger import ExecState, ExecutionLedger, compute_loop_id
from crawfish.output import Output, output_content_sha
from crawfish.store.sqlite import SqliteStore


def _store(tmp_path: Path) -> SqliteStore:
    return SqliteStore(tmp_path / "ledger.db")


def _ref() -> str:
    """A realistic output_ref: F-0's content sha of a frozen Output."""
    return output_content_sha(Output(value={"k": "v"}, produced_by="n1"))


# 1 + 4 — determinism / purity of compute_loop_id ------------------------------
def test_loop_id_is_deterministic_pure_function() -> None:
    a = compute_loop_id("bodysha", "item-lineage", "edge-1")
    b = compute_loop_id("bodysha", "item-lineage", "edge-1")
    assert a == b  # call twice -> equal
    assert len(a) == 64 and all(c in "0123456789abcdef" for c in a)  # plain sha256 hex
    # changing any input changes the id (no new_id() randomness in play)
    assert compute_loop_id("bodysha", "item-lineage", "edge-2") != a
    assert compute_loop_id("OTHER", "item-lineage", "edge-1") != a
    assert compute_loop_id("bodysha", "OTHER", "edge-1") != a
    # length-prefixing: ("ab","c",..) must not collide with ("a","bc",..)
    assert compute_loop_id("ab", "c", "e") != compute_loop_id("a", "bc", "e")


def test_two_invocations_same_loop_same_id(tmp_path: Path) -> None:
    """Two separate ExecutionLedger instances (process-like) over the same store
    derive the same loop_id for the same loop body + item + edge."""
    store = _store(tmp_path)
    led_a = ExecutionLedger(store)
    led_b = ExecutionLedger(store)  # second "process"
    lid_a = compute_loop_id("v1", "item-7", "back-edge")
    lid_b = compute_loop_id("v1", "item-7", "back-edge")
    assert lid_a == lid_b

    ref = _ref()
    led_a.checkpoint_iteration(lid_a, "item-7", "back-edge", 0, ref)
    # the other instance, re-deriving the same id, sees the same completed work
    assert led_b.completed_visits(lid_b, "item-7", "back-edge") == {0}
    store.close()


# 2 — resume re-charges $0 for completed iterations ----------------------------
def test_resume_skips_completed_visits(tmp_path: Path) -> None:
    store = _store(tmp_path)
    led = ExecutionLedger(store)
    lid = compute_loop_id("v1", "item-1", "e")
    for v in range(3):
        led.checkpoint_iteration(lid, "item-1", "e", v, _ref())

    # a fresh "process" resumes from the same store
    resumed = ExecutionLedger(store)
    done = resumed.completed_visits(lid, "item-1", "e")
    assert done == {0, 1, 2}

    # model the resume loop: only un-done visits would actually run (charge cost)
    planned = list(range(5))
    to_run = [v for v in planned if v not in done]
    assert to_run == [3, 4]  # 0,1,2 re-charge $0
    store.close()


def test_iteration_output_ref_round_trips(tmp_path: Path) -> None:
    store = _store(tmp_path)
    led = ExecutionLedger(store)
    lid = compute_loop_id("v1", "item-1", "e")
    ref = _ref()
    led.checkpoint_iteration(lid, "item-1", "e", 2, ref)
    assert led.iteration_output_ref(lid, "item-1", "e", 2) == ref
    assert led.iteration_output_ref(lid, "item-1", "e", 9) is None
    store.close()


# 3 — cross-org isolation ------------------------------------------------------
def test_cross_org_isolation(tmp_path: Path) -> None:
    store = _store(tmp_path)
    led_a = ExecutionLedger(store, org_id="a")
    led_b = ExecutionLedger(store, org_id="b")
    lid = compute_loop_id("v1", "item-1", "e")
    for v in range(3):
        led_a.checkpoint_iteration(lid, "item-1", "e", v, _ref())

    assert led_a.completed_visits(lid, "item-1", "e") == {0, 1, 2}
    assert led_b.completed_visits(lid, "item-1", "e") == set()  # org b sees NONE
    assert led_b.iteration_output_ref(lid, "item-1", "e", 0) is None
    store.close()


# 5 — depth variant for recurse ------------------------------------------------
def test_depth_variant_round_trips(tmp_path: Path) -> None:
    store = _store(tmp_path)
    led = ExecutionLedger(store)
    lid = compute_loop_id("v1", "item-1", "e")
    refs = {d: output_content_sha(Output(value={"d": d}, produced_by="n")) for d in range(3)}
    for d, ref in refs.items():
        led.checkpoint_depth(lid, "item-1", d, ref)

    assert led.completed_depths(lid, "item-1") == {0, 1, 2}
    for d, ref in refs.items():
        assert led.depth_output_ref(lid, "item-1", d) == ref
    assert led.depth_output_ref(lid, "item-1", 9) is None

    # depth and visit key spaces are distinct (no collision on the same loop/item)
    led.checkpoint_iteration(lid, "item-1", "e", 0, _ref())
    assert led.completed_visits(lid, "item-1", "e") == {0}
    assert led.completed_depths(lid, "item-1") == {0, 1, 2}  # unchanged
    store.close()


def test_depth_cross_org_isolation(tmp_path: Path) -> None:
    store = _store(tmp_path)
    led_a = ExecutionLedger(store, org_id="a")
    led_b = ExecutionLedger(store, org_id="b")
    lid = compute_loop_id("v1", "item-1", "e")
    led_a.checkpoint_depth(lid, "item-1", 0, _ref())
    assert led_a.completed_depths(lid, "item-1") == {0}
    assert led_b.completed_depths(lid, "item-1") == set()
    store.close()


# 6 — no regression to the linear pipeline ledger ------------------------------
def test_pipeline_ledger_still_works(tmp_path: Path) -> None:
    store = _store(tmp_path)
    led = ExecutionLedger(store)
    led.start_pipeline("pipe-1", "v1", total_items=2)
    assert led.pinned_version("pipe-1") == "v1"
    led.checkpoint_step("pipe-1", 0)
    led.checkpoint_step("pipe-1", 2)
    assert led.completed_steps("pipe-1") == {0, 2}

    # loop ledger writes must not bleed into the pipeline ledger namespace
    lid = compute_loop_id("v1", "item-1", "e")
    led.checkpoint_iteration(lid, "item-1", "e", 0, _ref())
    assert led.completed_steps("pipe-1") == {0, 2}  # untouched

    led.finish_pipeline("pipe-1", ExecState.DONE)
    rec = store.get_record("ledger_pipeline", "pipe-1")
    assert rec is not None and rec["status"] == ExecState.DONE.value
    store.close()
