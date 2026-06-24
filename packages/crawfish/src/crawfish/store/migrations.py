"""Versioned, migrate-on-open schema evolution for the SQLite ``Store``.

An older ``.crawfish`` database must upgrade cleanly when a newer Crawfish binary
opens it. We track the schema version in SQLite's built-in ``PRAGMA user_version``
(an atomic integer stored in the database header, free of an extra table) and apply
an ordered list of forward migrations on open.

Schema-version invariants
--------------------------
- A brand-new database starts at ``user_version = 0``.
- An *existing pre-versioning* database (tables already created by the old
  ``CREATE TABLE IF NOT EXISTS`` script, before this mechanism shipped) is also at
  ``user_version = 0`` — SQLite never set it.
- **Migration 1 is the baseline** = the original ``_SCHEMA``, written with idempotent
  ``CREATE TABLE IF NOT EXISTS``. So both the brand-new DB and the pre-versioning DB
  converge: migration 1 is a no-op on a DB that already has the tables, and creates
  them on a fresh DB. Every subsequent migration is a real forward step.
- Opening is idempotent: a fully-migrated DB applies nothing.
- A DB whose ``user_version`` exceeds :data:`CURRENT_SCHEMA_VERSION` is a *downgrade*
  (a newer binary wrote it). We refuse to open it — silently running an old binary
  against a newer schema risks corruption / data loss.

Authoring a migration (the contract)
------------------------------------
Phase-2 work that persists a new shape MUST do **two** things:

1. **Add a forward migration** here — append a :class:`Migration` with the next
   ascending ``version`` and bump :data:`CURRENT_SCHEMA_VERSION`. The migration body
   runs once, in a transaction, against an existing DB. It must be safe on a DB at any
   older version (use ``IF NOT EXISTS`` / additive ``ALTER TABLE``; never destructive
   rewrites). Migrations alter *structure*; they do not rewrite every JSON blob.

2. **Register a read-path up-converter** if the new shape changes how a stored record
   *kind* is interpreted — see :data:`RECORD_UPCONVERTERS`. A migration fixes the
   table; the up-converter lifts an individual legacy row's JSON envelope to the
   current shape lazily, on read, so historical rows stay readable without a bulk
   rewrite. This generalizes CRA-171's ``Emission.from_event`` shim from events to
   records.

Determinism: migration bodies must not depend on wall-clock time or randomness in a
way that affects schema. (The ``julianday('now')`` timestamp in ``put_record`` is data,
not schema, and is out of scope here.)
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass

from crawfish.core.types import JSONValue

__all__ = [
    "CURRENT_SCHEMA_VERSION",
    "MIGRATIONS",
    "RECORD_UPCONVERTERS",
    "Migration",
    "StoreMigrationError",
    "apply_migrations",
    "upconvert_record",
]


class StoreMigrationError(RuntimeError):
    """Raised when a database cannot be safely migrated on open.

    The load-bearing case is a **downgrade**: the on-disk ``user_version`` is greater
    than the binary's :data:`CURRENT_SCHEMA_VERSION`, meaning a newer Crawfish wrote
    this DB. We refuse rather than risk corrupting it.
    """


@dataclass(frozen=True)
class Migration:
    """One forward schema step, applied exactly once in a transaction.

    ``apply`` receives the open connection and performs DDL. It runs only when the
    DB's ``user_version`` is below ``version``. Keep bodies additive and idempotent
    (``IF NOT EXISTS``) so re-running across a partially-migrated DB is safe.
    """

    version: int
    description: str
    apply: Callable[[sqlite3.Connection], None]


# -- baseline (migration 1) ------------------------------------------------------
# Byte-for-byte the original table set, with IF NOT EXISTS so it is a no-op against a
# pre-versioning DB that already has the tables and a creator against a fresh DB.
_BASELINE_SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
    org_id TEXT NOT NULL,
    kind   TEXT NOT NULL,
    id     TEXT NOT NULL,
    json   TEXT NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (org_id, kind, id)
);
CREATE TABLE IF NOT EXISTS kv (
    org_id    TEXT NOT NULL,
    namespace TEXT NOT NULL,
    key       TEXT NOT NULL,
    json      TEXT NOT NULL,
    PRIMARY KEY (org_id, namespace, key)
);
CREATE TABLE IF NOT EXISTS idempotency (
    org_id TEXT NOT NULL,
    key    TEXT NOT NULL,
    PRIMARY KEY (org_id, key)
);
CREATE TABLE IF NOT EXISTS events (
    org_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    seq    INTEGER NOT NULL,
    json   TEXT NOT NULL,
    PRIMARY KEY (org_id, run_id, seq)
);
"""


def _migrate_baseline(conn: sqlite3.Connection) -> None:
    conn.executescript(_BASELINE_SCHEMA)


def _migrate_event_index(conn: sqlite3.Connection) -> None:
    """v2: index ``events(org_id, run_id)`` to speed the ledger read/append path.

    ``append_event`` and ``events`` both scan by ``(org_id, run_id)``. The primary key
    is ``(org_id, run_id, seq)`` so a covering index on the leading pair makes the
    ``MAX(seq)`` lookup and the ordered read sargable on large ledgers. Additive and
    idempotent.
    """
    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_org_run ON events(org_id, run_id)")


def _migrate_loop_ledger_index(conn: sqlite3.Connection) -> None:
    """v3: index ``records(org_id, kind)`` to speed the loop-ledger scan path (F-2).

    The loop/iteration ledger (CRA-195) introduces the ``ledger_loop`` record *kind*:
    composite-key rows ``(loop_id, item_id, edge_id, visit) -> output_ref`` plus a
    ``(loop_id, item_id, depth)`` variant for ``recurse``. ``completed_visits`` /
    ``completed_depths`` resolve completed iterations by scanning ``list_records`` for
    that kind within an ``org_id`` (resume must re-charge $0 for done iterations), so an
    index on the leading ``(org_id, kind)`` pair keeps that scan sargable as the loop
    ledger grows. The rows live in the generic ``records`` table (no new table needed —
    the namespace is a new *kind*, ``ledger_loop``), so this migration is purely the
    supporting index. Additive and idempotent; the existing ``ledger_pipeline`` /
    ``ledger_item`` / ``ledger_run`` kinds are untouched and keep working.
    """
    conn.execute("CREATE INDEX IF NOT EXISTS idx_records_org_kind ON records(org_id, kind)")


MIGRATIONS: list[Migration] = [
    Migration(1, "baseline schema (records, kv, idempotency, events)", _migrate_baseline),
    Migration(2, "index events(org_id, run_id) for ledger reads", _migrate_event_index),
    Migration(
        3,
        "index records(org_id, kind) for the loop ledger (F-2)",
        _migrate_loop_ledger_index,
    ),
]

#: The schema version this binary writes. Equals the highest migration version.
CURRENT_SCHEMA_VERSION: int = MIGRATIONS[-1].version


def apply_migrations(conn: sqlite3.Connection) -> None:
    """Bring ``conn`` up to :data:`CURRENT_SCHEMA_VERSION`, or refuse a downgrade.

    Reads ``PRAGMA user_version``; if it exceeds the current version, raises
    :class:`StoreMigrationError` (a newer binary wrote this DB). Otherwise applies every
    migration with ``version`` greater than the on-disk version, each in its own
    transaction, then stamps ``user_version``. Idempotent: a current DB applies nothing.

    The caller is responsible for holding any process-level lock. SQLite's own file lock
    plus the per-migration transaction keep a concurrent opener from corrupting state —
    the second opener observes the bumped ``user_version`` and applies nothing.
    """
    on_disk = int(conn.execute("PRAGMA user_version").fetchone()[0])

    if on_disk > CURRENT_SCHEMA_VERSION:
        raise StoreMigrationError(
            f"database schema version {on_disk} is newer than this build supports "
            f"({CURRENT_SCHEMA_VERSION}); refusing to open. Upgrade Crawfish."
        )

    if on_disk == CURRENT_SCHEMA_VERSION:
        return  # fully migrated; nothing to do

    for migration in MIGRATIONS:
        if migration.version <= on_disk:
            continue
        # Explicit BEGIN so the transaction covers DDL too. Python's stdlib sqlite3
        # driver only auto-opens a transaction before DML, NOT before CREATE/ALTER/DROP,
        # so a bare ``with conn:`` would leave each DDL statement autocommitting — a
        # multi-statement migration that failed midway would leave a half-applied schema.
        # ``PRAGMA user_version`` is stored in the DB header and is itself transactional,
        # so the version bump commits/rolls back atomically with the migration body.
        conn.execute("BEGIN")
        try:
            migration.apply(conn)
            # user_version cannot be parameterized; the value is our own int literal.
            conn.execute(f"PRAGMA user_version = {migration.version}")
        except Exception:
            conn.rollback()  # discard any partial DDL; user_version stays at the prior value
            raise
        conn.commit()


# -- read-path record up-conversion ----------------------------------------------
# Keyed by record ``kind``. A converter takes a decoded row dict and returns the
# current-shape dict. Empty today (identity for every kind); a new record kind that
# changes envelope shape registers its lifter here so legacy rows up-convert lazily on
# read in ``get_record`` / ``list_records`` — the record analogue of
# ``Emission.from_event``. Converters must be pure and deterministic.
RECORD_UPCONVERTERS: dict[str, Callable[[dict[str, JSONValue]], dict[str, JSONValue]]] = {}


def upconvert_record(kind: str, data: dict[str, JSONValue]) -> dict[str, JSONValue]:
    """Apply the registered up-converter for ``kind`` (identity if none registered)."""
    converter = RECORD_UPCONVERTERS.get(kind)
    return converter(data) if converter is not None else data
