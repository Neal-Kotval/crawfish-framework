"""CRA-103 acceptance: Source fan-out (single & multi) and credential safety.

Covers: single source emits exactly one Output, multi source fans out into one
Output per item, Outputs match the declared schema, and secret values never leak
into config or the serialized Output.
"""

from __future__ import annotations

import pytest

from crawfish.core.context import RunContext
from crawfish.core.types import Flow, NodeKind
from crawfish.nodes.source import PullRequestSource, RepoSource, fan_out
from crawfish.output import Output
from crawfish.store import SqliteStore


@pytest.fixture
def ctx() -> RunContext:
    return RunContext(store=SqliteStore(":memory:"))


def test_source_identity() -> None:
    src = RepoSource("repo", config={"repo": "owner/name"})
    assert src.kind is NodeKind.SOURCE
    assert src.name == "repo"
    assert src.id  # opaque id assigned


async def test_single_source_emits_one_output(ctx: RunContext) -> None:
    src = RepoSource("repo", config={"repo": "owner/name"})
    out = await src.fetch(ctx)
    assert out.produced_by == src.id
    assert out.value == {"repo": "owner/name"}
    # A single source fans out to exactly the one Output.
    fanned = src.fan_out(out)
    assert fanned == [out]
    assert len(fanned) == 1


async def test_single_source_output_matches_declared_schema(ctx: RunContext) -> None:
    src = RepoSource("repo", config={"repo": "owner/name"})
    out = await src.fetch(ctx)
    assert [(p.name, p.type) for p in out.output_schema] == [("repo", "str")]
    # config-derived param is STATIC.
    assert out.output_schema[0].flow is Flow.STATIC


async def test_multi_source_fans_out_per_item(ctx: RunContext) -> None:
    items = [
        {"number": 1, "title": "first"},
        {"number": 2, "title": "second"},
        {"number": 3, "title": "third"},
    ]
    src = PullRequestSource("prs", config={"repo": "owner/name", "items": items})
    out = await src.fetch(ctx)
    assert out.value == items

    fanned = src.fan_out(out)
    assert len(fanned) == len(items)
    # Each fanned Output carries one item, preserves produced_by, and the per-item schema.
    for fan, item in zip(fanned, items, strict=True):
        assert fan.value == item
        assert fan.produced_by == src.id
        assert [(p.name, p.type) for p in fan.output_schema] == [
            ("number", "int"),
            ("title", "str"),
        ]


async def test_multi_source_per_item_values_present(ctx: RunContext) -> None:
    items = [{"number": 7, "title": "lucky"}]
    src = PullRequestSource("prs", config={"repo": "r", "items": items})
    fanned = src.fan_out(await src.fetch(ctx))
    assert fanned[0].value["number"] == 7
    assert fanned[0].value["title"] == "lucky"


async def test_multi_source_empty_items(ctx: RunContext) -> None:
    src = PullRequestSource("prs", config={"repo": "r", "items": []})
    fanned = src.fan_out(await src.fetch(ctx))
    assert fanned == []


def test_fan_out_helper_single_passthrough() -> None:
    out: Output[str] = Output(value="x", produced_by="n1")
    assert fan_out(out, multi=False) == [out]


def test_fan_out_helper_multi_splits() -> None:
    out: Output[list[int]] = Output(value=[10, 20], produced_by="n1")
    fanned = fan_out(out, multi=True)
    assert [o.value for o in fanned] == [10, 20]
    assert all(o.produced_by == "n1" for o in fanned)


def test_fan_out_non_list_value_is_passthrough() -> None:
    # multi=True but value isn't a list: defensively pass through.
    out: Output[str] = Output(value="not-a-list", produced_by="n1")
    assert fan_out(out, multi=True) == [out]


async def test_no_secret_leak(ctx: RunContext, monkeypatch: pytest.MonkeyPatch) -> None:
    secret_value = "super-secret-token-value-xyz"
    monkeypatch.setenv("GITHUB_TOKEN", secret_value)

    src = RepoSource("repo", config={"repo": "owner/name", "auth": "GITHUB_TOKEN"})
    # config holds only the REFERENCE (env-var name), never the value.
    assert src.config["auth"] == "GITHUB_TOKEN"
    assert secret_value not in repr(src.config)

    out = await src.fetch(ctx)
    # The secret value must not appear in the serialized Output.
    assert secret_value not in out.model_dump_json()
    assert secret_value not in repr(out)
