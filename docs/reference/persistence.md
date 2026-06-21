# Persistence

How Crawfish remembers things across runs. Two swappable backends — one for small
typed records and key/value state, one for large blobs — plus the agent-facing
memory handle and the schema-upgrade machinery that keeps an old database readable.

**Symbols on this page:** `Store` · `SqliteStore` · `StoreMigrationError` ·
`Migration` · `CURRENT_SCHEMA_VERSION` · `Memory` · `ArtifactRef` · `ArtifactStore` ·
`LocalArtifactStore` · `offload_if_large`

---

## Core

A pipeline needs to remember things: which items it already handled, the telemetry
of a run, the outputs it produced. Crawfish routes all of that through two
**seams** — interchangeable backends that the rest of the framework talks to
through a contract, never by importing a specific database.

The **`Store`** is the first seam. It holds small, structured data: typed
**records** (a JSON blob filed under a `kind` and an `id`), a **key/value** store
for working state, an **idempotency** table for "have I done this exactly once?",
and an **event ledger** — an append-only log of what happened during a run.
`SqliteStore` is the local implementation, backed by an on-disk SQLite file (or an
in-memory database for tests).

Every `Store` row carries an **`org_id`** — a tenant key that defaults to
`"local"`. Two tenants can use the same namespace and key without seeing each
other's data; the `org_id` keeps them in separate partitions. On a single laptop
everything lands in `"local"` and you never think about it; in the cloud the same
code serves many tenants by passing a different `org_id`.

The **`ArtifactStore`** is the second seam, for **blobs** — files, images, big JSON
that has no business sitting inline in a record. Instead of the bytes, a record
carries an **`ArtifactRef`**: a small pointer (a content hash plus a URI and size).
The bytes live in the artifact store, addressed by their SHA-256 hash, so identical
content is stored once. `LocalArtifactStore` is the local implementation, writing
files under a directory. **`offload_if_large`** is the bridge: hand it a value and an
artifact store, and if the value is too big it spills the bytes to the store and
hands you back an `ArtifactRef` instead.

A **`Memory`** is the agent-facing handle over a `Store`. It scopes get/set to one
namespace and offers cross-run deduplication — `already_processed` /
`mark_processed` to remember handled items, and `claim` to win an item exactly once
even under concurrency.

When a newer Crawfish opens an older database, the schema may need upgrading.
**`Migration`** is one forward schema step; **`CURRENT_SCHEMA_VERSION`** is the
version this build writes; **`StoreMigrationError`** is raised when a database can't
be safely upgraded — most importantly when it was written by a *newer* binary than
the one trying to open it.

---

## Ramps up

### Why the product model imports a protocol, never a backend

`Store` and `ArtifactStore` are typed `Protocol`s. Nodes, the engine, and `Memory`
depend on the *protocol*, so swapping SQLite for Postgres, or local disk for S3, is
a driver swap rather than a rewrite. The discipline that makes this hold: **no raw
SQL appears at any call site** — it all lives inside `SqliteStore` — and **no
filesystem layout leaks out of `LocalArtifactStore`**. See
[ADR 0003](../architecture/decisions/0003-sqlite-wal-store.md) for the SQLite-WAL store decision.

### Tenancy is a key, not a schema

Every method on both seams takes a keyword-only `org_id: str = "local"`. In the
`Store` it is part of the primary key of every table (`records`, `kv`,
`idempotency`, `events`); in `LocalArtifactStore` it is a path prefix
(`root/<org_id>/<sha[:2]>/<sha>`). Multi-tenancy is therefore data partitioning,
not a migration: the same schema serves one tenant or thousands.

### Idempotency is a single atomic claim

`claim_idempotency` is one `INSERT OR IGNORE` against the `idempotency` table. The
insert either lands (you won the claim, `rowcount == 1`, returns `True`) or is
ignored because the key already exists (`False`). Check-and-write is a single
statement, so there is no window for two concurrent callers to both "win". This is
what makes `if mem.claim(id): process(id)` safe to run from many workers at once.

### WAL mode and the process lock

On a file-backed database, `SqliteStore` sets `PRAGMA journal_mode=WAL` so readers
never block writers — many fan-out workers can append telemetry and claim
idempotency keys concurrently. (WAL is skipped for `:memory:`, where it does not
apply.) A `threading.RLock` serializes access within a single process, and SQLite's
own file lock guards across processes. `PRAGMA synchronous=NORMAL` is set in both
modes.

### The event ledger assigns its own sequence

`append_event` does not trust the caller for ordering. It reads
`COALESCE(MAX(seq), -1) + 1` for the `(org_id, run_id)` and inserts at that
sequence, so events are densely numbered from 0 per run. `events` reads them back
`ORDER BY seq`. A v2 migration adds an index on `events(org_id, run_id)` to keep
both the max-lookup and the ordered read fast on large ledgers.

### Migrate-on-open, and the refusal to downgrade

The schema version lives in SQLite's built-in `PRAGMA user_version` (a transactional
integer in the database header — no extra table). On open, `apply_migrations` reads
it and applies every `Migration` whose `version` exceeds the on-disk value, each in
its own explicit `BEGIN`/`COMMIT` transaction, then stamps the new `user_version`.
A fresh database is at version 0; **migration 1 is the baseline** (the original table
set, written with `CREATE TABLE IF NOT EXISTS`) so a brand-new database and a
pre-versioning database converge to the same state. Opening is idempotent: a
fully-migrated database applies nothing.

If the on-disk `user_version` *exceeds* `CURRENT_SCHEMA_VERSION`, a newer binary
wrote this database, and `apply_migrations` raises `StoreMigrationError` rather than
risk corrupting it by running old code against a newer schema. See
[ADR 0014](../architecture/decisions/0014-store-schema-versioned-migrations.md) for the versioned-migration decision.

Migrations alter *structure*; they do not rewrite every stored JSON blob. When a
record `kind`'s envelope shape changes, a read-path **up-converter** (registered in
`RECORD_UPCONVERTERS`) lifts a single legacy row to the current shape lazily on
read in `get_record` / `list_records`. The registry is empty today (identity for
every kind).

### Why DDL needs an explicit `BEGIN`

Python's stdlib `sqlite3` driver auto-opens a transaction before DML but **not**
before `CREATE`/`ALTER`/`DROP`. A bare `with conn:` would let each DDL statement
autocommit, so a multi-statement migration that failed midway would leave a
half-applied schema. `apply_migrations` issues an explicit `BEGIN` so the whole
migration body plus the `user_version` bump commit or roll back as one unit.

### Artifacts are content-addressed and dedupe for free

`LocalArtifactStore.put` hashes the bytes with SHA-256, writes them atomically
(stage to a `.tmp` file, then `replace` into place), and returns an `ArtifactRef`
whose `uri` and `sha256` derive from that hash. Putting identical bytes twice writes
one file. `gc` sweeps a tenant's subtree and deletes any blob whose filename (its
sha) is not in the supplied `live_refs` set, returning the count removed.

`offload_if_large` serializes the value to JSON, and if the encoded form exceeds
`threshold` (default 65536 bytes) it stores the bytes with `content_type`
`application/json` and returns the `ArtifactRef`; otherwise it returns the value
unchanged. This keeps large Output payloads out of the `Store` record while small
values stay inline.

---

## API reference

### `Store`

`class Store(Protocol)` — the persistence contract: typed records, KV/working
memory, idempotency, and the event ledger. Every method takes a keyword-only
`org_id: str = "local"`.

```python
def put_record(self, kind: str, id: str, data: dict[str, JSONValue], *, org_id: str = "local") -> None
def get_record(self, kind: str, id: str, *, org_id: str = "local") -> dict[str, JSONValue] | None
def list_records(self, kind: str, *, org_id: str = "local") -> list[dict[str, JSONValue]]
def delete_record(self, kind: str, id: str, *, org_id: str = "local") -> None
def kv_get(self, namespace: str, key: str, *, org_id: str = "local") -> JSONValue | None
def kv_set(self, namespace: str, key: str, value: JSONValue, *, org_id: str = "local") -> None
def claim_idempotency(self, key: str, *, org_id: str = "local") -> bool
def append_event(self, run_id: str, event: dict[str, JSONValue], *, org_id: str = "local") -> None
def events(self, run_id: str, *, org_id: str = "local") -> list[dict[str, JSONValue]]
def close(self) -> None
```

| Method | Returns | Notes |
| --- | --- | --- |
| `put_record` | `None` | Upsert a JSON record under `(kind, id)`. |
| `get_record` | `dict \| None` | The record, up-converted on read, or `None`. |
| `list_records` | `list[dict]` | All records of `kind`, ordered by `updated_at`. |
| `delete_record` | `None` | Remove `(kind, id)`; no-op if absent. |
| `kv_get` | `JSONValue \| None` | Value at `(namespace, key)`, or `None`. |
| `kv_set` | `None` | Upsert a value at `(namespace, key)`. |
| `claim_idempotency` | `bool` | `True` iff this call won the claim (atomic). |
| `append_event` | `None` | Append to the run's ledger at the next `seq`. |
| `events` | `list[dict]` | The run's events, ordered by `seq`. |
| `close` | `None` | Release the backend connection. |

`@runtime_checkable`, so `isinstance(obj, Store)` checks method presence.

### `SqliteStore`

`class SqliteStore` — the local `Store`, backed by SQLite.

```python
def __init__(self, path: str | Path = ":memory:") -> None
```

Opens (or creates) the database at `path`, sets `PRAGMA synchronous=NORMAL` (and
`journal_mode=WAL` for a file path), then runs `apply_migrations` under an internal
lock. Use `":memory:"` for tests, a path for dev. Raises `StoreMigrationError` if
the on-disk schema is newer than this build.

### `Memory`

`class Memory` — a `Store`-backed KV/dedup handle scoped to `(namespace, org_id)`.

```python
def __init__(self, store: Store, namespace: str, *, org_id: str = "local") -> None

@classmethod
def for_run(cls, ctx: RunContext, namespace: str) -> Memory
```

| Method | Returns | Notes |
| --- | --- | --- |
| `for_run(ctx, namespace)` | `Memory` | Build from a `RunContext` (`ctx.store`, `ctx.org_id`). |
| `get(key)` | `JSONValue \| None` | Read working memory in this namespace. |
| `set(key, value)` | `None` | Write working memory in this namespace. |
| `already_processed(item_id)` | `bool` | `True` iff `mark_processed` was called for it. |
| `mark_processed(item_id)` | `None` | Record an item handled (persists across runs). |
| `claim(item_id)` | `bool` | Win an item exactly once, via `claim_idempotency`. |

Dedup state lives under a `seen:<id>` KV key; claims use a `<namespace>:claim:<id>`
idempotency key so different stages don't shadow one another.

### `ArtifactStore`

`class ArtifactStore(Protocol)` — the blob contract: content-addressed,
tenant-scoped, GC-able. Every method takes keyword-only `org_id: str = "local"`.

```python
def put(self, data: bytes, *, content_type: str = "application/octet-stream", org_id: str = "local") -> ArtifactRef
def get(self, ref: ArtifactRef, *, org_id: str = "local") -> bytes
def exists(self, ref: ArtifactRef, *, org_id: str = "local") -> bool
def delete(self, ref: ArtifactRef, *, org_id: str = "local") -> None
def gc(self, live_refs: set[str], *, org_id: str = "local") -> int
```

| Method | Returns | Notes |
| --- | --- | --- |
| `put` | `ArtifactRef` | Store bytes; identical content dedupes. |
| `get` | `bytes` | Bytes for `ref`; raises if absent for this `org_id`. |
| `exists` | `bool` | `True` iff `ref` is stored under this `org_id`. |
| `delete` | `None` | Remove `ref` for this `org_id` (no-op if absent). |
| `gc` | `int` | Delete blobs whose sha is not in `live_refs`; return count. |

`@runtime_checkable`.

### `ArtifactRef`

`class ArtifactRef(BaseModel)` — a content-addressed pointer carried by an Output
instead of inline bytes.

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `uri` | `str` | — (required) | Location of the bytes (e.g. a `file://` URI). |
| `sha256` | `str` | — (required) | SHA-256 of the content; identical content dedupes. |
| `size` | `int` | — (required) | Byte length of the content. |
| `content_type` | `str` | `"application/octet-stream"` | MIME type of the bytes. |

### `LocalArtifactStore`

`class LocalArtifactStore` — the local `ArtifactStore`, writing files under a root.

```python
def __init__(self, root: str | Path) -> None
```

Creates `root` if absent. Lays out content at `root/<org_id>/<sha[:2]>/<sha>`, so a
tenant is a path prefix and identical bytes resolve to one file. Writes are atomic
(temp file + `replace`).

### `offload_if_large`

```python
def offload_if_large(
    value: JSONValue,
    store: ArtifactStore,
    *,
    threshold: int = 65536,
    org_id: str = "local",
) -> JSONValue | ArtifactRef
```

Returns an `ArtifactRef` (`content_type` `application/json`) when `value`'s JSON
encoding exceeds `threshold` bytes; otherwise returns `value` unchanged. The
comparison is `len(data) <= threshold` — a value exactly at the threshold stays
inline.

### `Migration`

`@dataclass(frozen=True) class Migration` — one forward schema step, applied exactly
once in a transaction when the database's `user_version` is below `version`.

| Field | Type | Notes |
| --- | --- | --- |
| `version` | `int` | Ascending schema version this step reaches. |
| `description` | `str` | Human-readable summary. |
| `apply` | `Callable[[sqlite3.Connection], None]` | DDL body; keep additive and idempotent. |

### `CURRENT_SCHEMA_VERSION`

`CURRENT_SCHEMA_VERSION: int` — the schema version this build writes. Equals the
highest migration version; currently **`2`** (baseline + an `events` index). A
database whose `user_version` exceeds this is a downgrade and is refused.

### `StoreMigrationError`

`class StoreMigrationError(RuntimeError)` — raised by `apply_migrations` when a
database cannot be safely migrated on open. The load-bearing case is a downgrade:
the on-disk `user_version` is greater than `CURRENT_SCHEMA_VERSION`, so a newer
Crawfish wrote the database and this build refuses to open it.

---

## Example

A single in-memory `SqliteStore`: round-trip a KV value, show that `org_id` isolates
tenants, claim an idempotency key once, and spill a large value to a
`LocalArtifactStore` as an `ArtifactRef`.

```python
import tempfile
from crawfish.store.sqlite import SqliteStore
from crawfish.store.migrations import CURRENT_SCHEMA_VERSION
from crawfish.artifacts.base import ArtifactRef
from crawfish.artifacts.local import LocalArtifactStore, offload_if_large

store = SqliteStore(":memory:")
print("schema", CURRENT_SCHEMA_VERSION)

# KV round-trip.
store.kv_set("triage", "last_pr", 42)
print("kv_get", store.kv_get("triage", "last_pr"))

# org_id tenancy: same namespace + key, two tenants, no bleed-through.
store.kv_set("triage", "owner", "acme", org_id="acme")
print("default org", store.kv_get("triage", "owner"))            # isolated -> None
print("acme org", store.kv_get("triage", "owner", org_id="acme"))

# Idempotency: the first claim wins, the second does not.
print("claim #1", store.claim_idempotency("pr-7"))
print("claim #2", store.claim_idempotency("pr-7"))

# Artifacts: a small value stays inline; a large one offloads to a ref.
blobs = LocalArtifactStore(tempfile.mkdtemp())
print("small", offload_if_large("hello", blobs))
big = offload_if_large(["x"] * 20000, blobs)
print("offloaded", isinstance(big, ArtifactRef), big.content_type, big.size > 65536)
store.close()
```

??? success "▶ Output"

    ```text
    schema 2
    kv_get 42
    default org None
    acme org acme
    claim #1 True
    claim #2 False
    small hello
    offloaded True application/json True
    ```
