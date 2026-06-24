"""CRA-191 acceptance: versioned migrate-on-open for the SQLite Store.

Covers: fresh DB reaches CURRENT_SCHEMA_VERSION with working CRUD; round-trip across
two versions preserves data; downgrade is refused; re-open is idempotent; a
pre-versioning DB migrates without data loss; and a registered RECORD_UPCONVERTERS
entry up-converts a legacy row on read.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from crawfish.store import (
    CURRENT_SCHEMA_VERSION,
    SqliteStore,
    StoreMigrationError,
)
from crawfish.store import migrations as mig
from crawfish.store.migrations import _BASELINE_SCHEMA


def _user_version(path: Path) -> int:
    conn = sqlite3.connect(str(path))
    try:
        return int(conn.execute("PRAGMA user_version").fetchone()[0])
    finally:
        conn.close()


def _has_index(path: Path, name: str) -> bool:
    conn = sqlite3.connect(str(path))
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?", (name,)
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def test_fresh_memory_db_at_current_version_with_working_crud() -> None:
    s = SqliteStore()
    assert int(s._conn.execute("PRAGMA user_version").fetchone()[0]) == CURRENT_SCHEMA_VERSION
    # all tables present + basic CRUD across each
    s.put_record("run", "r1", {"a": 1})
    assert s.get_record("run", "r1") == {"a": 1}
    s.kv_set("memory", "k", True)
    assert s.kv_get("memory", "k") is True
    assert s.claim_idempotency("once") is True
    s.append_event("r1", {"event": "start"})
    assert [e["event"] for e in s.events("r1")] == ["start"]


def test_round_trip_across_two_versions_preserves_data(tmp_path: Path) -> None:
    """Force a baseline-only (v1) DB, write a row, reopen -> migrates to v2, row survives."""
    db = tmp_path / "rt.crawfish"
    conn = sqlite3.connect(str(db))
    conn.executescript(_BASELINE_SCHEMA)
    conn.execute(
        "INSERT INTO records(org_id, kind, id, json, updated_at) VALUES(?,?,?,?,?)",
        ("local", "run", "r1", '{"a": 1}', 1.0),
    )
    conn.execute("PRAGMA user_version = 1")  # baseline only; no v2 index
    conn.commit()
    conn.close()

    assert _user_version(db) == 1
    assert not _has_index(db, "idx_events_org_run")

    s = SqliteStore(db)
    try:
        assert _user_version(db) == CURRENT_SCHEMA_VERSION
        assert _has_index(db, "idx_events_org_run")  # the v2 artifact now exists
        assert s.get_record("run", "r1") == {"a": 1}  # pre-existing row survived
    finally:
        s.close()


def test_downgrade_is_refused(tmp_path: Path) -> None:
    db = tmp_path / "newer.crawfish"
    s = SqliteStore(db)
    s.close()
    conn = sqlite3.connect(str(db))
    conn.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION + 1}")
    conn.commit()
    conn.close()

    with pytest.raises(StoreMigrationError):
        SqliteStore(db)


def test_reopen_is_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "idem.crawfish"
    s = SqliteStore(db)
    s.put_record("run", "r1", {"a": 1})
    s.close()

    assert _user_version(db) == CURRENT_SCHEMA_VERSION
    s2 = SqliteStore(db)
    try:
        assert _user_version(db) == CURRENT_SCHEMA_VERSION  # unchanged
        assert s2.get_record("run", "r1") == {"a": 1}  # data intact
    finally:
        s2.close()


def test_preversioning_db_migrates_without_data_loss(tmp_path: Path) -> None:
    """Tables created by the raw baseline script with user_version=0 (never set)."""
    db = tmp_path / "legacy.crawfish"
    conn = sqlite3.connect(str(db))
    conn.executescript(_BASELINE_SCHEMA)
    conn.execute(
        "INSERT INTO records(org_id, kind, id, json, updated_at) VALUES(?,?,?,?,?)",
        ("local", "run", "old", '{"legacy": true}', 1.0),
    )
    conn.commit()
    conn.close()
    assert _user_version(db) == 0  # pre-versioning

    s = SqliteStore(db)
    try:
        assert _user_version(db) == CURRENT_SCHEMA_VERSION
        assert s.get_record("run", "old") == {"legacy": True}
    finally:
        s.close()


def test_failing_multistatement_migration_rolls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """A migration whose 2nd statement fails must leave NO partial schema and NOT bump
    the version (the per-migration transaction covers DDL, not just DML)."""

    def good_then_bad(conn: sqlite3.Connection) -> None:
        conn.execute("CREATE TABLE half_applied (x INTEGER)")  # 1st: succeeds
        conn.execute("INSERT INTO does_not_exist VALUES (1)")  # 2nd: raises

    monkeypatch.setattr(
        mig,
        "MIGRATIONS",
        [*mig.MIGRATIONS, mig.Migration(3, "intentionally failing", good_then_bad)],
    )
    monkeypatch.setattr(mig, "CURRENT_SCHEMA_VERSION", 3)

    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("PRAGMA user_version = 2")  # start fully migrated to the real current
        with pytest.raises(sqlite3.OperationalError):
            mig.apply_migrations(conn)
        # the 1st statement's table was rolled back with the failing transaction...
        leaked = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='half_applied'"
        ).fetchone()
        assert leaked is None
        # ...and the version stayed at the last good migration.
        assert int(conn.execute("PRAGMA user_version").fetchone()[0]) == 2
    finally:
        conn.close()


@pytest.fixture
def _temp_upconverter() -> Iterator[None]:
    """Register a temp up-converter for kind 'legacy_kind', cleaned up after."""

    def lift(data: dict[str, object]) -> dict[str, object]:
        out = dict(data)
        out.setdefault("envelope_version", 1)
        out["upconverted"] = True
        return out

    mig.RECORD_UPCONVERTERS["legacy_kind"] = lift  # type: ignore[assignment]
    try:
        yield
    finally:
        del mig.RECORD_UPCONVERTERS["legacy_kind"]


def test_registered_upconverter_lifts_legacy_row_on_read(_temp_upconverter: None) -> None:
    s = SqliteStore()
    s.put_record("legacy_kind", "x", {"raw": 1})  # stored in legacy shape
    got = s.get_record("legacy_kind", "x")
    assert got == {"raw": 1, "envelope_version": 1, "upconverted": True}
    # list_records applies it too
    listed = s.list_records("legacy_kind")
    assert listed == [{"raw": 1, "envelope_version": 1, "upconverted": True}]
    # an unregistered kind is identity
    s.put_record("run", "r", {"a": 1})
    assert s.get_record("run", "r") == {"a": 1}
