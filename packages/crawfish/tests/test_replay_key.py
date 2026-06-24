"""CRA-194 / F-1 acceptance: canonical cassette-key / execution-coordinate schema.

``_key`` is the execution coordinate of a run. These tests pin the legacy key
byte-for-byte (back-compat for unsalted cassettes) and prove that the new folded
components — :class:`ExecutionCoordinate`, ``org_id``, and a decode-control field —
each move the key, while their absence leaves it identical to today's.

All deterministic: no model calls. We exercise ``_key`` directly (the pure identity
function) plus the runtime ``run()`` plumbing of the optional coordinate.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from types import SimpleNamespace

from crawfish.core.context import RunContext
from crawfish.definition import Definition
from crawfish.runtime import RunRequest
from crawfish.runtime.replay import ExecutionCoordinate, _key
from crawfish.store import SqliteStore

FIXTURES = Path(__file__).parent / "fixtures"

# Pinned legacy key for a fixed RunRequest (role="scout", inputs={"pr_body": "x"}) over
# the `full` fixture Definition, with NO coordinate / org="local" / no decode field.
# Computed from the pre-F-1 code; this MUST never move or legacy cassettes break.
LEGACY_KEY = "8dd9f4eb30b6b0ed"


def _definition(tmp_path: Path) -> Definition:
    dest = tmp_path / "full"
    shutil.copytree(FIXTURES / "full", dest)
    return Definition.from_package(str(dest))


def _request(d: Definition) -> RunRequest:
    return RunRequest(definition=d, role="scout", inputs={"pr_body": "x"})


# --- AC 3: legacy back-compat (byte-for-byte) ----------------------------------------


def test_legacy_key_is_byte_for_byte_stable(tmp_path: Path) -> None:
    """No coordinate, org='local', no decode field ⇒ the exact pre-F-1 key."""
    key = _key(_request(_definition(tmp_path)))
    assert key == LEGACY_KEY


def test_empty_coordinate_does_not_salt_key(tmp_path: Path) -> None:
    """An all-None coordinate is *absent* — it must fold nothing."""
    req = _request(_definition(tmp_path))
    assert _key(req, coordinate=ExecutionCoordinate()) == LEGACY_KEY
    assert _key(req, org_id="local", coordinate=None) == LEGACY_KEY


# --- AC 1: k quorum samples ⇒ k distinct keys ----------------------------------------


def test_k_quorum_samples_produce_distinct_keys(tmp_path: Path) -> None:
    req = _request(_definition(tmp_path))
    k = 5
    keys = {_key(req, coordinate=ExecutionCoordinate(sample_index=i)) for i in range(k)}
    assert len(keys) == k
    # ...and none collide with the legacy (sample_index=None) key.
    assert LEGACY_KEY not in keys


# --- AC 2: same (version, inputs, coordinate) replays identically ---------------------


def test_same_coordinate_replays_identically(tmp_path: Path) -> None:
    req = _request(_definition(tmp_path))
    coord = ExecutionCoordinate(sample_index=2)
    assert _key(req, coordinate=coord) == _key(req, coordinate=ExecutionCoordinate(sample_index=2))


# --- AC 4: cross-tenant isolation -----------------------------------------------------


def test_cross_tenant_keys_differ(tmp_path: Path) -> None:
    req = _request(_definition(tmp_path))
    assert _key(req, org_id="a") != _key(req, org_id="b")
    # 'local' is the default sentinel and must equal the unsalted legacy key.
    assert _key(req, org_id="local") == LEGACY_KEY
    assert _key(req, org_id="a") != LEGACY_KEY


# --- AC 5: each coordinate axis is distinct ------------------------------------------


def test_coordinate_axes_are_distinct(tmp_path: Path) -> None:
    req = _request(_definition(tmp_path))
    by_axis = {
        "sample_index": _key(req, coordinate=ExecutionCoordinate(sample_index=1)),
        "iter_index": _key(req, coordinate=ExecutionCoordinate(iter_index=1)),
        "visit_count": _key(req, coordinate=ExecutionCoordinate(visit_count=1)),
        "depth": _key(req, coordinate=ExecutionCoordinate(depth=1)),
    }
    assert len(set(by_axis.values())) == 4, by_axis


# --- AC 6: decode field moves the key, absent leaves it unchanged ---------------------


def test_decode_field_changes_key_when_present(tmp_path: Path) -> None:
    d = _definition(tmp_path)
    base = _request(d)
    # Simulate F-5's concurrently-added RunRequest.decode_seed via a duck-typed stand-in
    # that carries the same identity fields plus the decode field.
    with_seed = SimpleNamespace(
        definition=base.definition,
        inputs=base.inputs,
        role=base.role,
        model=base.model,
        session_id=base.session_id,
        decode_seed=1234,
    )
    other_seed = SimpleNamespace(
        definition=base.definition,
        inputs=base.inputs,
        role=base.role,
        model=base.model,
        session_id=base.session_id,
        decode_seed=5678,
    )
    # Absent decode field ⇒ legacy key (base RunRequest has no decode_seed attr).
    assert _key(base) == LEGACY_KEY
    # Present decode field ⇒ different key, and distinct seeds ⇒ distinct keys.
    assert _key(with_seed) != LEGACY_KEY  # type: ignore[arg-type]
    assert _key(with_seed) != _key(other_seed)  # type: ignore[arg-type]


def test_decode_seed_none_is_treated_as_absent(tmp_path: Path) -> None:
    base = _request(_definition(tmp_path))
    explicit_none = SimpleNamespace(
        definition=base.definition,
        inputs=base.inputs,
        role=base.role,
        model=base.model,
        session_id=base.session_id,
        decode_seed=None,
    )
    assert _key(explicit_none) == LEGACY_KEY  # type: ignore[arg-type]


# --- runtime run() plumbing of the coordinate ----------------------------------------


async def test_run_threads_coordinate_into_cassette(tmp_path: Path) -> None:
    """Recording two coordinates writes two distinct cassettes; replay hits each."""
    from crawfish.runtime import MockRuntime, RecordReplayRuntime

    d = _definition(tmp_path)
    req = _request(d)
    ctx = RunContext(store=SqliteStore())
    cdir = tmp_path / "cassettes"

    rec = RecordReplayRuntime(MockRuntime(), cdir, record=True)
    await rec.run(req, ctx, coordinate=ExecutionCoordinate(sample_index=0))
    await rec.run(req, ctx, coordinate=ExecutionCoordinate(sample_index=1))

    written = sorted(p.stem for p in cdir.glob("*.json"))
    assert len(written) == 2
    expected = sorted(
        _key(req, org_id=ctx.org_id, coordinate=ExecutionCoordinate(sample_index=i)) for i in (0, 1)
    )
    assert written == expected

    # Replay (record=False) must hit the exact recorded coordinate.
    play = RecordReplayRuntime(MockRuntime(), cdir, record=False)
    res = await play.run(req, ctx, coordinate=ExecutionCoordinate(sample_index=1))
    assert res is not None
