"""UNFILED-DEPLOY — ``craw code deploy`` (+ default observers) and ``craw code fleet``.

Pins the M4.5 deploy/fleet veneer: ``deploy`` composes ``crawfish.deploy.deploy`` (no second
supervisor) and scaffolds default cost/failure/stuck observers only when ``observers/`` is
empty; the spawned argv carries only name/dir/schedule — **never a secret** (operate-layer
rule 2); ``fleet`` mirrors ``manage_list``; and a deploy in org A is invisible to ``fleet
--org b`` (tenancy). An injectable ``spawn`` seam means no real daemon is launched.
"""

from __future__ import annotations

from pathlib import Path

from crawfish.code.deploy import (
    deploy_pipeline,
    fleet_rows,
    scaffold_default_observers,
)


def _store(tmp_path: Path):
    from crawfish.store import SqliteStore

    return SqliteStore(tmp_path / "fleet.db")


def _spawn_seam():
    """A spawn seam that records argv instead of launching a daemon. Returns (spawn, calls)."""
    calls: list[list[str]] = []

    def _spawn(argv: list[str], cwd: Path, log: Path) -> int:
        calls.append(list(argv))
        return 4242  # a fake but plausible pid

    return _spawn, calls


def test_deploy_composes_supervisor_and_scaffolds_observers(tmp_path: Path) -> None:
    """deploy spawns via the shipped path and scaffolds the three default observers."""
    project = tmp_path / "proj"
    project.mkdir()
    store = _store(tmp_path)
    spawn, calls = _spawn_seam()

    body = deploy_pipeline(
        "triage-bot",
        project_dir=project,
        store=store,
        schedule="0 8 * * *",
        org_id="local",
        spawn=spawn,
    )
    assert body["pipeline"] == "triage-bot"
    assert body["session"] == "crawfish/triage-bot"
    assert sorted(body["observers_scaffolded"]) == ["cost_spike", "failure_rate", "stuck"]
    # The three default observers landed as authored watcher packages.
    for name in ("cost_spike", "failure_rate", "stuck"):
        assert (project / "observers" / name / "instructions.md").exists()
    assert len(calls) == 1  # exactly one detached child spawned
    store.close()


def test_spawned_argv_carries_no_secret(tmp_path: Path) -> None:
    """The detached child's argv carries only name/dir/schedule — never a secret value."""
    project = tmp_path / "proj"
    project.mkdir()
    store = _store(tmp_path)
    spawn, calls = _spawn_seam()
    deploy_pipeline(
        "triage-bot",
        project_dir=project,
        store=store,
        schedule="0 8 * * *",
        org_id="local",
        spawn=spawn,
    )
    (argv,) = calls
    # The argv names the pipeline, its dir, and the schedule — and nothing else sensitive.
    assert "triage-bot" in argv
    assert "0 8 * * *" in argv
    # No secret-shaped token rides in argv (the operate-layer rule-2 red line).
    joined = " ".join(argv).lower()
    for forbidden in ("token", "secret", "password", "api_key", "sk-"):
        assert forbidden not in joined
    store.close()


def test_observers_never_clobbered(tmp_path: Path) -> None:
    """An authored observers/ dir is never overwritten (scaffold only when empty)."""
    project = tmp_path / "proj"
    (project / "observers" / "mine").mkdir(parents=True)
    (project / "observers" / "mine" / "instructions.md").write_text("authored\n")
    scaffolded = scaffold_default_observers(project)
    assert scaffolded == []  # nothing scaffolded over the authored watcher
    assert (project / "observers" / "mine" / "instructions.md").read_text() == "authored\n"


def test_observers_none_skips_scaffold(tmp_path: Path) -> None:
    """--observers none deploys without scaffolding any default watcher."""
    project = tmp_path / "proj"
    project.mkdir()
    store = _store(tmp_path)
    spawn, _ = _spawn_seam()
    body = deploy_pipeline(
        "p", project_dir=project, store=store, observers="none", org_id="local", spawn=spawn
    )
    assert body["observers_scaffolded"] == []
    assert not (project / "observers").exists()
    store.close()


def test_fleet_mirrors_manage_list(tmp_path: Path) -> None:
    """fleet rows mirror manage_list (name/status/cost) for the deployed pipeline."""
    project = tmp_path / "proj"
    project.mkdir()
    store = _store(tmp_path)
    spawn, _ = _spawn_seam()
    deploy_pipeline("triage-bot", project_dir=project, store=store, org_id="local", spawn=spawn)
    rows = fleet_rows(store, org_id="local")
    names = {r["name"] for r in rows}
    assert "triage-bot" in names
    row = next(r for r in rows if r["name"] == "triage-bot")
    assert set(row) == {"name", "status", "uptime_s", "next_fire", "cost_today_usd"}
    store.close()


def test_two_org_isolation(tmp_path: Path) -> None:
    """A deploy in org A is invisible to fleet --org b (tenancy)."""
    project = tmp_path / "proj"
    project.mkdir()
    store = _store(tmp_path)
    spawn, _ = _spawn_seam()
    deploy_pipeline("a-only", project_dir=project, store=store, org_id="a", spawn=spawn)
    assert any(r["name"] == "a-only" for r in fleet_rows(store, org_id="a"))
    assert not any(r["name"] == "a-only" for r in fleet_rows(store, org_id="b"))
    store.close()
