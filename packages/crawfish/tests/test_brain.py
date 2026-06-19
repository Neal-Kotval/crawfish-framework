"""CRA-111 acceptance: the Company Brain registry over the Store.

Verifies the Brain lists every configured Source / authored Definition / produced
Output, that version-pinned Definition lookup returns the exact artifact, that
Output ``produced_by`` provenance survives a round-trip, that the index persists
(a fresh Brain over the same Store still finds everything), and that tenancy is
enforced by ``org_id``.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from crawfish.brain import CompanyBrain
from crawfish.core.types import Parameter
from crawfish.definition import Definition
from crawfish.nodes import PullRequestSource, RepoSource
from crawfish.output import Output
from crawfish.store.sqlite import SqliteStore

FIXTURES = Path(__file__).parent / "fixtures"


def _full_definition(tmp_path: Path) -> Definition:
    target = tmp_path / "full"
    shutil.copytree(FIXTURES / "full", target, dirs_exist_ok=True)
    return Definition.from_package(str(target))


def test_register_and_lookup(tmp_path: Path) -> None:
    brain = CompanyBrain(SqliteStore())
    d = _full_definition(tmp_path)
    repo = RepoSource("repo")
    out = Output(
        output_schema=[Parameter(name="repo", type="str")],
        value={"repo": "owner/name"},
        produced_by=repo.id,
    )

    brain.register_definition(d)
    brain.register_source(repo)
    brain.register_output(out)

    found = brain.definition(d.id)
    assert found is not None
    assert found["id"] == d.id

    caps = brain.sources_by_capability("repo")
    assert [s["id"] for s in caps] == [repo.id]

    produced = brain.outputs_by_producer(repo.id)
    assert [o["id"] for o in produced] == [out.id]


def test_lists_every_entity(tmp_path: Path) -> None:
    brain = CompanyBrain(SqliteStore())
    d = _full_definition(tmp_path)
    repo = RepoSource("repo")
    prs = PullRequestSource("prs")

    brain.register_definition(d)
    brain.register_source(repo)
    brain.register_source(prs)

    assert len(brain.list_definitions()) == 1
    assert {s["id"] for s in brain.list_sources()} == {repo.id, prs.id}


def test_version_pinned_lookup_returns_exact_artifact(tmp_path: Path) -> None:
    brain = CompanyBrain(SqliteStore())
    d = _full_definition(tmp_path)
    brain.register_definition(d)

    pinned = brain.definition(d.id, version=str(d.version))
    assert pinned is not None
    assert pinned["version"] == str(d.version)
    # A non-existent version pin returns nothing (no fuzzy fallback).
    assert brain.definition(d.id, version="99.99") is None


def test_capability_distinguishes_sources() -> None:
    brain = CompanyBrain(SqliteStore())
    repo = RepoSource("repo")
    prs = PullRequestSource("prs")
    brain.register_source(repo)
    brain.register_source(prs)

    # RepoSource declares a "repo" output; PullRequestSource does not.
    assert [s["id"] for s in brain.sources_by_capability("repo")] == [repo.id]
    assert {s["id"] for s in brain.sources_by_capability("number")} == {prs.id}


def test_provenance_survives_round_trip() -> None:
    brain = CompanyBrain(SqliteStore())
    repo = RepoSource("repo")
    out = Output(
        output_schema=[Parameter(name="repo", type="str")],
        value={"repo": "owner/name"},
        produced_by=repo.id,
    )
    brain.register_output(out)

    (record,) = brain.outputs_by_producer(repo.id)
    assert record["produced_by"] == repo.id
    assert record["id"] == out.id


def test_persistence_across_brains(tmp_path: Path) -> None:
    store = SqliteStore()
    d = _full_definition(tmp_path)
    repo = RepoSource("repo")
    out = Output(
        output_schema=[Parameter(name="repo", type="str")],
        value={"repo": "owner/name"},
        produced_by=repo.id,
    )

    first = CompanyBrain(store)
    first.register_definition(d)
    first.register_source(repo)
    first.register_output(out)

    # A NEW Brain over the SAME Store sees everything registered before.
    second = CompanyBrain(store)
    assert second.definition(d.id) is not None
    assert [s["id"] for s in second.sources_by_capability("repo")] == [repo.id]
    assert [o["id"] for o in second.outputs_by_producer(repo.id)] == [out.id]


def test_tenancy_isolates_orgs(tmp_path: Path) -> None:
    store = SqliteStore()
    d = _full_definition(tmp_path)
    repo = RepoSource("repo")

    acme = CompanyBrain(store, org_id="acme")
    acme.register_definition(d)
    acme.register_source(repo)

    # The default org cannot see acme's entities.
    default = CompanyBrain(store)
    assert default.definition(d.id) is None
    assert default.list_sources() == []
    # But acme still can.
    assert acme.definition(d.id) is not None
    assert [s["id"] for s in acme.list_sources()] == [repo.id]
