"""Global deployment index: `craw manage` aggregates every project from anywhere."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from crawfish.deploy import DeployEntry, DeployRegistry
from crawfish.manage import (
    global_manage_list,
    read_deployments,
    register_deployment,
    resolve_deployment_dir,
)
from crawfish.observe import ObserverSurface, RunInfo
from crawfish.store import SqliteStore

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def test_register_and_read_is_idempotent_by_name(tmp_path: Path) -> None:
    idx = tmp_path / "deployments.json"
    register_deployment("a", "/projects/a", path=idx)
    register_deployment("b", "/projects/b", path=idx)
    register_deployment("a", "/projects/a-moved", path=idx)  # update, not duplicate
    entries = read_deployments(path=idx)
    assert [e["name"] for e in entries] == ["a", "b"]
    assert resolve_deployment_dir("a", path=idx) == "/projects/a-moved"
    assert resolve_deployment_dir("missing", path=idx) is None


def test_read_missing_index_is_empty(tmp_path: Path) -> None:
    assert read_deployments(path=tmp_path / "nope.json") == []


def _seed_project(store: SqliteStore, name: str) -> None:
    DeployRegistry(store).register(
        DeployEntry(name=name, pid=999999, dir=f"/p/{name}", session=f"crawfish/{name}")
    )
    ObserverSurface(store).put_run_info(
        RunInfo(pipeline=name, run_id=f"{name}-run", status="done", started_at=NOW.timestamp())
    )


def test_global_manage_list_aggregates_across_stores(tmp_path: Path) -> None:
    idx = tmp_path / "deployments.json"
    stores = {"/p/alpha": SqliteStore(), "/p/beta": SqliteStore()}
    _seed_project(stores["/p/alpha"], "alpha")
    _seed_project(stores["/p/beta"], "beta")
    register_deployment("alpha", "/p/alpha", path=idx)
    register_deployment("beta", "/p/beta", path=idx)

    rows = global_manage_list(open_store=lambda d: stores[d], path=idx, now=NOW)
    assert [r.name for r in rows] == ["alpha", "beta"]  # aggregated + sorted
    assert all(r.last_run_status == "done" for r in rows)


def test_global_manage_list_skips_unreadable_project(tmp_path: Path) -> None:
    idx = tmp_path / "deployments.json"
    ok = SqliteStore()
    _seed_project(ok, "good")
    register_deployment("good", "/p/good", path=idx)
    register_deployment("broken", "/p/broken", path=idx)

    def opener(d: str) -> SqliteStore:
        if d == "/p/broken":
            raise RuntimeError("store unreadable")
        return ok

    rows = global_manage_list(open_store=opener, path=idx, now=NOW)
    assert [r.name for r in rows] == ["good"]  # broken project skipped, not fatal
