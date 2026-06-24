"""Execution-state ledger + durability reconciliation.

Makes runs/pipelines crash-safe and re-runnable regardless of backend. The ledger
lives in the ``Store``: per-pipeline status + **version pin** + fan-out **item cursor**,
and per-run status tagged with its backend. On restart, :meth:`reconcile` resumes
resumable runs and marks orphaned CommandRuntime (`claude -p`) runs failed→retry —
they die with the engine and must never be silently lost. An in-flight pipeline stays
pinned to the version it started; a redeploy applies to new pipelines only.
"""

from __future__ import annotations

import hashlib
from enum import Enum

from crawfish.store.base import Store

__all__ = ["ExecState", "ExecutionLedger", "compute_loop_id"]

# Version tag for the loop_id derivation. Bump if the composition of the digest
# inputs ever changes (it changes every derived loop_id, so it is a migration).
_LOOP_ID_VERSION = 1


def compute_loop_id(body_version_sha: str, item_lineage: str, edge_id: str) -> str:
    """Deterministic identity for a loop instance — a pure function of its inputs.

    ``loop_id = sha256(body_version_sha + item_lineage + edge_id)``. This is a **hard
    requirement**: a loop's id is derived, never minted with ``new_id()``, so that two
    independent process invocations of the same loop body over the same item along the
    same back-edge re-derive the *same* id and resume re-charges $0 for iterations that
    already completed. Inputs are length-prefixed so distinct concatenations cannot
    collide (e.g. ``("ab","c")`` vs ``("a","bc")``).
    """
    parts = (str(_LOOP_ID_VERSION), body_version_sha, item_lineage, edge_id)
    payload = "\x00".join(f"{len(p)}:{p}" for p in parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class ExecState(str, Enum):
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    NEEDS_RETRY = "needs_retry"


# Backends whose sessions die with the engine (cannot be resumed after a crash).
_EPHEMERAL_BACKENDS = {"command", "mock"}


class ExecutionLedger:
    """Store-backed execution state for pipelines, runs, and fan-out items."""

    def __init__(self, store: Store, *, org_id: str = "local") -> None:
        self._store = store
        self._org = org_id

    # -- pipelines ----------------------------------------------------------
    def start_pipeline(self, pipeline_id: str, version: str, *, total_items: int = 0) -> None:
        self._store.put_record(
            "ledger_pipeline",
            pipeline_id,
            {
                "id": pipeline_id,
                "version": version,  # pinned for the life of this pipeline
                "status": ExecState.RUNNING.value,
                "total_items": total_items,
                "completed_steps": [],
            },
            org_id=self._org,
        )

    def pinned_version(self, pipeline_id: str) -> str | None:
        """The version this pipeline started on — unchanged by any redeploy."""
        rec = self._store.get_record("ledger_pipeline", pipeline_id, org_id=self._org)
        return None if rec is None else str(rec["version"])

    def checkpoint_step(self, pipeline_id: str, step_index: int) -> None:
        rec = self._store.get_record("ledger_pipeline", pipeline_id, org_id=self._org)
        if rec is None:
            return
        steps = set(rec.get("completed_steps") or [])
        steps.add(step_index)
        rec["completed_steps"] = sorted(steps)
        self._store.put_record("ledger_pipeline", pipeline_id, rec, org_id=self._org)

    def completed_steps(self, pipeline_id: str) -> set[int]:
        rec = self._store.get_record("ledger_pipeline", pipeline_id, org_id=self._org)
        return set(rec.get("completed_steps") or []) if rec else set()

    def finish_pipeline(self, pipeline_id: str, status: ExecState = ExecState.DONE) -> None:
        rec = self._store.get_record("ledger_pipeline", pipeline_id, org_id=self._org)
        if rec is not None:
            rec["status"] = status.value
            self._store.put_record("ledger_pipeline", pipeline_id, rec, org_id=self._org)

    # -- fan-out item cursor -----------------------------------------------
    def mark_item(self, pipeline_id: str, item_id: str, status: ExecState) -> None:
        self._store.put_record(
            "ledger_item",
            f"{pipeline_id}:{item_id}",
            {"pipeline_id": pipeline_id, "item_id": item_id, "status": status.value},
            org_id=self._org,
        )

    def completed_items(self, pipeline_id: str) -> set[str]:
        return {
            str(r["item_id"])
            for r in self._store.list_records("ledger_item", org_id=self._org)
            if r["pipeline_id"] == pipeline_id and r["status"] == ExecState.DONE.value
        }

    # -- loop / recurse iteration ledger (composite key) --------------------
    # An EXTENDED key space, distinct from the linear pipeline ledger above. The
    # pipeline ledger tracks ``step_index: int`` per pipeline; a loop must track
    # progress per ``(loop_id, item_id, edge_id, visit)`` (and a per-item depth
    # stack for ``recurse``). Each iteration records the frozen Output reference it
    # produced (``output_content_sha`` from F-0). Every row carries ``org_id`` so
    # cross-tenant resume cannot see another org's completed iterations.
    #
    # All raw persistence stays in the Store: these methods only orchestrate the
    # generic record API under the ``ledger_loop`` namespace. The composite key is
    # encoded into the record id; ``loop_id`` (already a hex digest) and the other
    # components are joined with a delimiter that cannot appear in a hex digest.

    @staticmethod
    def _visit_key(loop_id: str, item_id: str, edge_id: str, visit: int) -> str:
        return f"v|{loop_id}|{item_id}|{edge_id}|{visit}"

    @staticmethod
    def _depth_key(loop_id: str, item_id: str, depth: int) -> str:
        return f"d|{loop_id}|{item_id}|{depth}"

    def checkpoint_iteration(
        self, loop_id: str, item_id: str, edge_id: str, visit: int, output_ref: str
    ) -> None:
        """Record that ``visit`` of this loop over this item completed, pinning the
        frozen Output reference (``output_content_sha``) it produced. Idempotent:
        re-checkpointing the same coordinate overwrites with the same ref."""
        self._store.put_record(
            "ledger_loop",
            self._visit_key(loop_id, item_id, edge_id, visit),
            {
                "loop_id": loop_id,
                "item_id": item_id,
                "edge_id": edge_id,
                "visit": visit,
                "output_ref": output_ref,
            },
            org_id=self._org,
        )

    def completed_visits(self, loop_id: str, item_id: str, edge_id: str) -> set[int]:
        """The visit indices already recorded for ``(loop_id, item_id, edge_id)`` in
        this org. Resume loads these and skips them (re-charges $0)."""
        return {
            int(r["visit"])
            for r in self._store.list_records("ledger_loop", org_id=self._org)
            if r.get("loop_id") == loop_id
            and r.get("item_id") == item_id
            and r.get("edge_id") == edge_id
            and "visit" in r
        }

    def iteration_output_ref(
        self, loop_id: str, item_id: str, edge_id: str, visit: int
    ) -> str | None:
        """The frozen Output reference recorded for a specific completed visit."""
        rec = self._store.get_record(
            "ledger_loop", self._visit_key(loop_id, item_id, edge_id, visit), org_id=self._org
        )
        return None if rec is None else str(rec["output_ref"])

    def checkpoint_depth(self, loop_id: str, item_id: str, depth: int, output_ref: str) -> None:
        """The ``recurse`` variant: record completion at a given ``depth`` of the
        per-item recursion stack, pinning the Output reference it produced."""
        self._store.put_record(
            "ledger_loop",
            self._depth_key(loop_id, item_id, depth),
            {
                "loop_id": loop_id,
                "item_id": item_id,
                "depth": depth,
                "output_ref": output_ref,
            },
            org_id=self._org,
        )

    def completed_depths(self, loop_id: str, item_id: str) -> set[int]:
        """The recursion depths already recorded for ``(loop_id, item_id)`` in this org."""
        return {
            int(r["depth"])
            for r in self._store.list_records("ledger_loop", org_id=self._org)
            if r.get("loop_id") == loop_id and r.get("item_id") == item_id and "depth" in r
        }

    def depth_output_ref(self, loop_id: str, item_id: str, depth: int) -> str | None:
        """The frozen Output reference recorded at a specific recursion ``depth``."""
        rec = self._store.get_record(
            "ledger_loop", self._depth_key(loop_id, item_id, depth), org_id=self._org
        )
        return None if rec is None else str(rec["output_ref"])

    # -- per-run state ------------------------------------------------------
    def record_run(self, run_id: str, *, backend: str, status: ExecState, version: str) -> None:
        self._store.put_record(
            "ledger_run",
            run_id,
            {"id": run_id, "backend": backend, "status": status.value, "version": version},
            org_id=self._org,
        )

    # -- restart recovery ---------------------------------------------------
    def reconcile(self) -> dict[str, list[str]]:
        """Reconcile orphaned state after an engine restart.

        Runs still ``RUNNING`` on an ephemeral backend (their subprocess died with
        the engine) are marked ``NEEDS_RETRY``; resumable-backend runs are left for
        resume. Returns the run ids in each bucket.
        """
        retried: list[str] = []
        resumable: list[str] = []
        for rec in self._store.list_records("ledger_run", org_id=self._org):
            if rec["status"] != ExecState.RUNNING.value:
                continue
            run_id = str(rec["id"])
            if rec["backend"] in _EPHEMERAL_BACKENDS:
                rec["status"] = ExecState.NEEDS_RETRY.value
                self._store.put_record("ledger_run", run_id, rec, org_id=self._org)
                retried.append(run_id)
            else:
                resumable.append(run_id)
        return {"retried": retried, "resumable": resumable}
