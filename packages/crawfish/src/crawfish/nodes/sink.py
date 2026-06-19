"""Sink nodes — the egress boundary with idempotency + approval gates (CRA-104).

A ``Sink`` is the only place a pipeline performs an external side effect (open a
PR, post a comment). Three invariants make egress safe:

* **Static-only targets.** Destination slots (repo, project, channel) must be
  ``Flow.STATIC`` — chosen once at batch start, never derived from a fluid /
  model-influenced value. A ``Flow.FLUID`` target is rejected at construction
  (wire time), so a prompt can never redirect a write.
* **Idempotency.** Every write is keyed by a hash of *static config only* plus
  the batch + output identity. A re-run of the same batch is a no-op, not a
  duplicate side effect. The key never depends on fluid/model-derived data.
* **Approval gate.** ``always_ask`` sinks refuse to fire without an explicit
  human approval callback.

Credentials are held **by reference** (an env-var name in config), never by
value: no secret reaches stored config, the ``Output``, logs, or telemetry.
"""

from __future__ import annotations

import hashlib
import json
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Generic

from crawfish.core.context import RunContext
from crawfish.core.ids import new_id
from crawfish.core.types import Flow, JSONValue, Node, NodeKind, Parameter, T
from crawfish.output import Output

# An approval callback returns True to allow the write, False to skip it.
ApproveCallback = Callable[[], bool]

__all__ = [
    "TargetMustBeStaticError",
    "ApprovalRequired",
    "Sink",
    "LinearSink",
    "GitHubPRSink",
    "ApproveCallback",
]


class TargetMustBeStaticError(ValueError):
    """Raised when a target parameter is ``Flow.FLUID``.

    Targets address *where* a write lands; allowing a fluid (per-item,
    model-influenced) target would let upstream data redirect egress. Rejected
    at construction so the guarantee holds at wire/compile time, not runtime.
    """


class ApprovalRequired(RuntimeError):
    """Raised when an ``always_ask`` sink is asked to write without approval."""


class Sink(Node, ABC, Generic[T]):
    """Base class for egress nodes. Subclasses implement :meth:`_write`.

    The public :meth:`write` wraps the side effect with idempotency and the
    optional approval gate; subclasses never reimplement those invariants.
    """

    def __init__(
        self,
        name: str,
        config: dict[str, JSONValue] | None = None,
        *,
        always_ask: bool = False,
        target_params: list[Parameter] | None = None,
    ) -> None:
        self.id = new_id()
        self.name = name
        self.kind = NodeKind.SINK
        self.config: dict[str, JSONValue] = dict(config or {})
        self.always_ask = always_ask
        self.target_params: list[Parameter] = list(target_params or [])

        # Static-only targets: reject any fluid target at construction time.
        for param in self.target_params:
            if param.flow is not Flow.STATIC:
                raise TargetMustBeStaticError(
                    f"target parameter {param.name!r} on sink {name!r} must be "
                    f"Flow.STATIC (got {param.flow.value!r}); fluid targets are "
                    "rejected to prevent model-influenced redirection of egress"
                )

    # -- the actual side effect --------------------------------------------
    @abstractmethod
    async def _write(self, output: Output[T], ctx: RunContext) -> None:
        """Perform the external side effect. Implemented by concrete sinks."""

    # -- idempotency key (static config only) ------------------------------
    def _idempotency_key(self, output: Output[T], ctx: RunContext) -> str:
        """Derive the idempotency key from STATIC config + batch/output identity.

        Deliberately excludes the ``Output`` value and any fluid/model-derived
        data: re-running the same batch yields the same key (a no-op), while a
        different prompt/value cannot escape idempotency by perturbing the key.
        """
        payload = {
            "sink": self.name,
            "batch_id": ctx.batch_id,
            "output_id": output.id,
            # Sorted static config so key is stable regardless of dict order.
            "static_config": json.dumps(self.config, sort_keys=True, default=str),
        }
        material = json.dumps(payload, sort_keys=True)
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    # -- public, invariant-enforcing entry point ---------------------------
    async def write(
        self,
        output: Output[T],
        ctx: RunContext,
        *,
        approve: ApproveCallback | None = None,
    ) -> bool:
        """Write ``output`` to this sink's static target.

        Returns ``True`` if the side effect ran, ``False`` if it was skipped
        (already written, or approval declined). Raises :class:`ApprovalRequired`
        when an ``always_ask`` sink is invoked without an ``approve`` callback.
        """
        key = self._idempotency_key(output, ctx)

        # Atomically claim the key. If we didn't win, this batch/output was
        # already written — no-op.
        if not ctx.store.claim_idempotency(key, org_id=ctx.org_id):
            return False

        # Approval gate.
        if self.always_ask:
            if approve is None:
                raise ApprovalRequired(
                    f"sink {self.name!r} requires approval but no approve callback was provided"
                )
            if not approve():
                return False

        await self._write(output, ctx)

        # Telemetry: record that a write happened. Never include the credential
        # value or the (possibly model-derived) output value.
        ctx.store.append_event(
            ctx.run_id,
            {
                "type": "sink.write",
                "sink": self.name,
                "node_id": self.id,
                "output_id": output.id,
                "batch_id": ctx.batch_id,
                "idempotency_key": key,
            },
            org_id=ctx.org_id,
        )
        return True


def _credential_ref(config: dict[str, JSONValue]) -> str | None:
    """Read the env-var *name* (a reference, never a value) from config."""
    ref = config.get("credential_ref")
    return ref if isinstance(ref, str) else None


class LinearSink(Sink[JSONValue]):
    """Create a Linear issue/comment. Dry-run by default (network-free).

    In ``dry_run`` mode the would-be write is recorded into :attr:`writes`
    instead of hitting the network, which keeps tests deterministic.
    """

    def __init__(
        self,
        name: str = "linear",
        config: dict[str, JSONValue] | None = None,
        *,
        always_ask: bool = False,
        target_params: list[Parameter] | None = None,
        dry_run: bool = True,
    ) -> None:
        super().__init__(name, config, always_ask=always_ask, target_params=target_params)
        self.dry_run = dry_run
        self.writes: list[dict[str, JSONValue]] = []

    async def _write(self, output: Output[JSONValue], ctx: RunContext) -> None:
        # Resolve the credential by reference only at egress; never store value.
        ref = _credential_ref(self.config)
        record: dict[str, JSONValue] = {
            "kind": "linear_issue",
            "team": self.config.get("team"),
            "project": self.config.get("project"),
            "output_id": output.id,
            "value": output.value,
            "credential_ref": ref,  # the NAME, not the secret value
        }
        if self.dry_run:
            self.writes.append(record)
            return
        # Live path (intentionally unimplemented in the reference sink).
        raise NotImplementedError("LinearSink live mode is not implemented")


class GitHubPRSink(Sink[JSONValue]):
    """Open a GitHub pull request. Dry-run by default (network-free).

    In ``dry_run`` mode the would-be PR is recorded into :attr:`writes` instead
    of calling GitHub, keeping tests deterministic and offline.
    """

    def __init__(
        self,
        name: str = "github_pr",
        config: dict[str, JSONValue] | None = None,
        *,
        always_ask: bool = False,
        target_params: list[Parameter] | None = None,
        dry_run: bool = True,
    ) -> None:
        super().__init__(name, config, always_ask=always_ask, target_params=target_params)
        self.dry_run = dry_run
        self.writes: list[dict[str, JSONValue]] = []

    async def _write(self, output: Output[JSONValue], ctx: RunContext) -> None:
        ref = _credential_ref(self.config)
        record: dict[str, JSONValue] = {
            "kind": "github_pr",
            "repo": self.config.get("repo"),
            "base": self.config.get("base"),
            "output_id": output.id,
            "value": output.value,
            "credential_ref": ref,  # the NAME, not the secret value
        }
        if self.dry_run:
            self.writes.append(record)
            return
        raise NotImplementedError("GitHubPRSink live mode is not implemented")
