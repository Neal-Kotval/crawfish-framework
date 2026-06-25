"""CRA-266 — per-file authorship provenance + taint (the M0 keystone).

The per-Definition :class:`~crawfish.provenance.Provenance` is too coarse for ``craw
code``, which authors at *file* granularity. These tests pin the new per-file row:
human vs agent authorship of identical bytes, tainted authorship surviving a re-record
(monotonicity, SECURITY.md rule 9), per-org isolation, the ``file.authored`` emission,
and the versioned ``craw.code.provenance.v1`` payload shape. In-memory ``SqliteStore``;
no model call.
"""

from __future__ import annotations

from crawfish.emission import EmissionKind, read_emissions
from crawfish.jail import FLUID_TAINT
from crawfish.provenance import (
    FILE_PROVENANCE_RECORD_KIND,
    PROVENANCE_RECORD_KIND,
    FileProvenance,
    component_tainted,
    file_provenance,
    record_file_provenance,
)
from crawfish.store import SqliteStore


def test_records_one_row_per_path_and_sha() -> None:
    """A row persists per (component_path, content_sha) and reads back, carrying org_id."""
    store = SqliteStore()
    try:
        prov = record_file_provenance(
            "tools/notify.py", "ab12", store=store, authored_by="craw-code", org_id="acme"
        )
        assert isinstance(prov, FileProvenance)
        got = file_provenance("tools/notify.py", "ab12", store=store, org_id="acme")
        assert got is not None
        assert got.component_path == "tools/notify.py"
        assert got.content_sha == "ab12"
        assert got.authored_by == "craw-code"
    finally:
        store.close()


def test_human_and_agent_share_sha_but_differ_in_authored_by() -> None:
    """Identical bytes (same sha) carry the same content identity but distinct authorship.

    The sha is a pure content hash — ``authored_by`` is adjacent to identity, never mixed
    in — so a human copy and an agent copy of identical bytes share a sha but the row
    distinguishes who wrote it.
    """
    store = SqliteStore()
    try:
        # Two files at distinct paths with the SAME bytes (same sha), different authors.
        human = record_file_provenance("tools/a.py", "deadbeef", store=store, authored_by="human")
        agent = record_file_provenance(
            "tools/b.py", "deadbeef", store=store, authored_by="craw-code"
        )
        assert human.content_sha == agent.content_sha == "deadbeef"
        assert human.authored_by == "human"
        assert agent.authored_by == "craw-code"
        # Neither is tainted; the human one is never tainted by default.
        assert not human.taint
        assert not human.source_tainted
    finally:
        store.close()


def test_fluid_authorship_sets_taint_and_component_tainted_is_true() -> None:
    """A file authored under fluid context is stamped tainted and carries FLUID_TAINT."""
    store = SqliteStore()
    try:
        prov = record_file_provenance(
            "tools/exfil.py",
            "c0ffee",
            store=store,
            authored_by="craw-code",
            source_tainted=True,
        )
        assert prov.source_tainted is True
        assert FLUID_TAINT in prov.taint
        assert component_tainted("tools/exfil.py", "c0ffee", store=store) is True
        # An unknown file (no row) is NOT tainted — it is outside the provenance scope.
        assert component_tainted("tools/unknown.py", "c0ffee", store=store) is False
    finally:
        store.close()


def test_taint_is_monotonic_across_a_re_record() -> None:
    """Re-recording a previously tainted file never drops the label (rule 9, monotonic)."""
    store = SqliteStore()
    try:
        record_file_provenance(
            "tools/x.py", "aa11", store=store, authored_by="craw-code", source_tainted=True
        )
        # A naive clean re-record of the SAME (path, sha) must NOT launder the taint away.
        again = record_file_provenance(
            "tools/x.py", "aa11", store=store, authored_by="craw-code", source_tainted=False
        )
        assert FLUID_TAINT in again.taint
        assert again.source_tainted is True
        assert component_tainted("tools/x.py", "aa11", store=store) is True
    finally:
        store.close()


def test_per_org_isolation() -> None:
    """Two orgs do not see each other's per-file rows."""
    store = SqliteStore()
    try:
        record_file_provenance(
            "tools/secret.py", "f00d", store=store, authored_by="craw-code", org_id="orgA"
        )
        # org B sees nothing of org A's.
        assert file_provenance("tools/secret.py", "f00d", store=store, org_id="orgB") is None
        assert component_tainted("tools/secret.py", "f00d", store=store, org_id="orgB") is False
        # org A still sees its own.
        assert file_provenance("tools/secret.py", "f00d", store=store, org_id="orgA") is not None
    finally:
        store.close()


def test_per_definition_provenance_kind_is_untouched() -> None:
    """The per-file record kind is distinct from the per-Definition kind (no collision)."""
    assert FILE_PROVENANCE_RECORD_KIND != PROVENANCE_RECORD_KIND
    store = SqliteStore()
    try:
        record_file_provenance("tools/n.py", "ab12", store=store, authored_by="craw-code")
        # No per-Definition provenance row was written by the per-file path.
        assert store.list_records(PROVENANCE_RECORD_KIND) == []
        assert len(store.list_records(FILE_PROVENANCE_RECORD_KIND)) == 1
    finally:
        store.close()


def test_file_authored_emission_mirrors_tainted() -> None:
    """A METRIC ``file.authored`` emission is written with ``tainted`` mirroring source_tainted."""
    store = SqliteStore()
    try:
        record_file_provenance(
            "tools/notify.py",
            "ab12",
            store=store,
            authored_by="craw-code",
            source_tainted=True,
        )
        emissions = read_emissions(store, "authored:ab12")
        metrics = [e for e in emissions if e.kind is EmissionKind.METRIC]
        assert metrics, "no file.authored METRIC emission written"
        e = metrics[0]
        assert e.attrs["metric"] == "file.authored"
        assert e.attrs["tainted"] is True
        assert e.tainted is True
        assert e.is_valid()  # carries the required ('metric', 'value') attrs
    finally:
        store.close()


def test_provenance_v1_payload_snapshot() -> None:
    """Snapshot the craw.code.provenance.v1 projection (consumed by describe/sync)."""
    store = SqliteStore()
    try:
        prov = record_file_provenance(
            "tools/notify.py",
            "ab12",
            store=store,
            authored_by="craw-code",
            source_tainted=True,
        )
        payload = {
            "schema": "craw.code.provenance.v1",
            "component": prov.component_path,
            "content_sha": prov.content_sha,
            "authored_by": prov.authored_by,
            "source_tainted": prov.source_tainted,
            "taint": sorted(prov.taint),
        }
        assert payload == {
            "schema": "craw.code.provenance.v1",
            "component": "tools/notify.py",
            "content_sha": "ab12",
            "authored_by": "craw-code",
            "source_tainted": True,
            "taint": ["fluid"],
        }
    finally:
        store.close()
