"""Acceptance tests for ``output_content_sha`` (CRA-193 / F-0).

The helper is a pure SHA-256 over an ``Output``'s *content* (value, schema,
producer, lineage, taint) — deliberately excluding the volatile per-instance
``id`` — so structurally-equal Outputs hash equal and the digest is stable
across processes.
"""

from __future__ import annotations

import re

from crawfish.core.types import Flow, Parameter
from crawfish.output import Output, output_content_sha


def _schema() -> list[Parameter]:
    return [Parameter(name="body", type="str", flow=Flow.FLUID)]


def _output(**overrides: object) -> Output[object]:
    kwargs: dict[str, object] = {
        "value": {"text": "hello", "n": 1},
        "produced_by": "node-1",
        "output_schema": _schema(),
        "lineage": "item-7",
        "tainted": False,
    }
    kwargs.update(overrides)
    return Output(**kwargs)


def test_digest_is_hex_sha256() -> None:
    sha = output_content_sha(_output())
    assert re.fullmatch(r"[0-9a-f]{64}", sha)


def test_stable_for_same_output() -> None:
    o = _output()
    assert output_content_sha(o) == output_content_sha(o)


def test_stable_across_reconstructed_equal_output() -> None:
    # Acceptance #1: deterministic / canonical — a freshly reconstructed but
    # structurally-equal Output hashes identically (proxy for cross-process).
    assert output_content_sha(_output()) == output_content_sha(_output())


def test_structurally_equal_with_different_id_hash_equal() -> None:
    # Acceptance #2 + #4: id is volatile and excluded from the hash.
    a = _output()
    b = _output()
    assert a.id != b.id  # fresh UUID per instance
    assert output_content_sha(a) == output_content_sha(b)

    explicit = _output(id="forced-distinct-id")
    assert explicit.id == "forced-distinct-id"
    assert output_content_sha(explicit) == output_content_sha(a)


def test_value_change_changes_digest() -> None:
    assert output_content_sha(_output(value={"text": "hello", "n": 1})) != output_content_sha(
        _output(value={"text": "hello", "n": 2})
    )


def test_taint_change_changes_digest() -> None:
    assert output_content_sha(_output(tainted=False)) != output_content_sha(_output(tainted=True))


def test_lineage_change_changes_digest() -> None:
    assert output_content_sha(_output(lineage="item-7")) != output_content_sha(
        _output(lineage="item-8")
    )


def test_produced_by_change_changes_digest() -> None:
    assert output_content_sha(_output(produced_by="node-1")) != output_content_sha(
        _output(produced_by="node-2")
    )


def test_schema_change_changes_digest() -> None:
    other = [Parameter(name="title", type="str", flow=Flow.FLUID)]
    assert output_content_sha(_output()) != output_content_sha(_output(output_schema=other))


def test_canonical_dict_key_order_independent() -> None:
    # Acceptance #1: canonical JSON sorts keys, so semantically-equal values with
    # differently-ordered dict keys hash the same.
    a = _output(value={"a": 1, "b": 2})
    b = _output(value={"b": 2, "a": 1})
    assert output_content_sha(a) == output_content_sha(b)


def test_derive_propagates_taint_and_distinct_id() -> None:
    # Acceptance #3: existing derive() behavior must still hold.
    parent = _output(tainted=True, lineage="item-9")
    child = parent.derive(value={"text": "world"}, produced_by="node-2")
    assert child.id != parent.id
    assert child.tainted is True  # taint propagated
    assert child.lineage == "item-9"  # lineage propagated


def test_derive_result_hashes_stably() -> None:
    parent = _output(tainted=True)
    c1 = parent.derive(value={"text": "world"}, produced_by="node-2")
    c2 = parent.derive(value={"text": "world"}, produced_by="node-2")
    assert c1.id != c2.id
    assert output_content_sha(c1) == output_content_sha(c2)
