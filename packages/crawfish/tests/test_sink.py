"""Tests for the Sink framework: idempotency, approval, static targets (CRA-104)."""

from __future__ import annotations

import json

import pytest

from crawfish.core.context import RunContext
from crawfish.core.types import Flow, Parameter
from crawfish.nodes.sink import (
    ApprovalRequired,
    GitHubPRSink,
    LinearSink,
    TargetMustBeStaticError,
)
from crawfish.output import Output
from crawfish.store import SqliteStore

SECRET_ENV_NAME = "GITHUB_TOKEN"
SECRET_VALUE = "ghp_super_secret_value_should_never_leak"


def _ctx() -> RunContext:
    """One shared in-memory store with a fixed batch_id."""
    return RunContext(store=SqliteStore(), batch_id="b1")


def _output() -> Output[dict[str, str]]:
    return Output(value={"title": "Fix bug"}, produced_by="node-x")


async def test_write_is_recorded_in_dry_run() -> None:
    ctx = _ctx()
    sink = GitHubPRSink(config={"repo": "acme/widget", "credential_ref": SECRET_ENV_NAME})
    out = _output()

    wrote = await sink.write(out, ctx)

    assert wrote is True
    assert len(sink.writes) == 1
    assert sink.writes[0]["repo"] == "acme/widget"


async def test_second_identical_write_is_noop() -> None:
    ctx = _ctx()
    sink = GitHubPRSink(config={"repo": "acme/widget", "credential_ref": SECRET_ENV_NAME})
    out = _output()  # same output.id, same batch_id -> same idempotency key

    first = await sink.write(out, ctx)
    second = await sink.write(out, ctx)

    assert first is True
    assert second is False
    assert len(sink.writes) == 1  # only one PR recorded


async def test_fluid_target_rejected_at_construction() -> None:
    fluid_target = Parameter(name="repo", type="str", flow=Flow.FLUID)
    with pytest.raises(TargetMustBeStaticError):
        GitHubPRSink(target_params=[fluid_target])


def test_static_target_accepted() -> None:
    static_target = Parameter(name="repo", type="str", flow=Flow.STATIC)
    sink = GitHubPRSink(target_params=[static_target])
    assert sink.target_params[0].flow is Flow.STATIC


async def test_gated_sink_without_approval_raises() -> None:
    ctx = _ctx()
    sink = LinearSink(config={"team": "ENG"}, always_ask=True)
    with pytest.raises(ApprovalRequired):
        await sink.write(_output(), ctx)


async def test_gated_sink_with_approval_writes() -> None:
    ctx = _ctx()
    sink = LinearSink(config={"team": "ENG"}, always_ask=True)
    wrote = await sink.write(_output(), ctx, approve=lambda: True)
    assert wrote is True
    assert len(sink.writes) == 1


async def test_gated_sink_with_declined_approval_skips() -> None:
    ctx = _ctx()
    sink = LinearSink(config={"team": "ENG"}, always_ask=True)
    wrote = await sink.write(_output(), ctx, approve=lambda: False)
    assert wrote is False
    assert sink.writes == []


async def test_no_credential_value_leak() -> None:
    ctx = _ctx()
    sink = GitHubPRSink(config={"repo": "acme/widget", "credential_ref": SECRET_ENV_NAME})
    out = _output()
    await sink.write(out, ctx)

    # Recorded write holds only the env-var NAME, never the secret value.
    record_blob = json.dumps(sink.writes, default=str)
    assert SECRET_VALUE not in record_blob
    assert SECRET_ENV_NAME in record_blob

    # Config repr never contains the secret value.
    assert SECRET_VALUE not in repr(sink.config)

    # Telemetry events never contain the secret value.
    events_blob = json.dumps(ctx.store.events(ctx.run_id), default=str)
    assert SECRET_VALUE not in events_blob


async def test_idempotency_key_ignores_output_value() -> None:
    # Two outputs differing only in value but sharing id -> same key (no-op).
    ctx = _ctx()
    sink = GitHubPRSink(config={"repo": "acme/widget"})
    out_a: Output[dict[str, str]] = Output(id="shared", value={"v": "a"}, produced_by="n")
    out_b: Output[dict[str, str]] = Output(id="shared", value={"v": "b"}, produced_by="n")

    assert await sink.write(out_a, ctx) is True
    assert await sink.write(out_b, ctx) is False
    assert len(sink.writes) == 1
