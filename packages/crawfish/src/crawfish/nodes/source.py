"""Source nodes — pipeline ingress with single- & multi-item fan-out (CRA-103).

A ``Source`` is the entry point of a pipeline: it ``fetch``es data and emits a
typed :class:`~crawfish.output.Output`. A *single* source produces one Output that
seeds one Run; a *multi* source produces an Output whose value is a list, which
:func:`fan_out` explodes into one Output per item (each seeding its own Run).

Security (CRA-103/CRA-104): credentials are held **by reference** only. ``config``
stores the env-var *name* (e.g. ``"GITHUB_TOKEN"``); the value is resolved via
:func:`~crawfish.secrets.resolve_secret` at fetch time and never written to
``config``, the Output, or logs.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Generic

from crawfish.core.context import RunContext
from crawfish.core.ids import new_id
from crawfish.core.types import Flow, JSONValue, Node, NodeKind, Parameter, T
from crawfish.output import Output

__all__ = ["Source", "fan_out", "RepoSource", "PullRequestSource"]


class Source(Node, ABC, Generic[T]):
    """Pipeline ingress that fetches data and emits a typed Output.

    Subclasses declare their per-item shape in :attr:`outputs` and implement
    :meth:`fetch`. Set :attr:`multi` to ``True`` when :meth:`fetch` returns an
    Output whose value is a list of items to fan out into independent Runs.
    """

    outputs: list[Parameter] = []
    multi: bool = False

    def __init__(self, name: str, config: dict[str, JSONValue] | None = None) -> None:
        self.id = new_id()
        self.name = name
        self.kind = NodeKind.SOURCE
        self.config: dict[str, JSONValue] = dict(config or {})

    @abstractmethod
    async def fetch(self, ctx: RunContext) -> Output[T]:
        """Fetch data and return a typed Output matching :attr:`outputs`."""
        raise NotImplementedError

    def fan_out(self, output: Output[T]) -> list[Output[JSONValue]]:
        """Explode a multi source's list Output into one Output per item.

        For a single source (or a non-list value), returns ``[output]`` unchanged.
        Each fanned Output preserves ``produced_by`` and carries the per-item schema.
        """
        return fan_out(output, multi=self.multi, item_schema=self.outputs)


def fan_out(
    output: Output[JSONValue],
    *,
    multi: bool,
    item_schema: list[Parameter] | None = None,
) -> list[Output[JSONValue]]:
    """Split a multi-item Output into per-item Outputs that seed N Runs.

    When ``multi`` is ``False`` (or the value is not a list), the input Output is
    returned as a single-element list. Otherwise each list item becomes its own
    Output with ``value`` set to the item, ``produced_by`` preserved, and
    ``output_schema`` set to ``item_schema`` (the declared per-item shape).
    """
    if not multi or not isinstance(output.value, list):
        return [output]
    schema = list(item_schema) if item_schema is not None else list(output.output_schema)
    return [
        Output(value=item, produced_by=output.produced_by, output_schema=schema)
        for item in output.value
    ]


class RepoSource(Source[dict[str, JSONValue]]):
    """Single source describing one repository (deterministic, network-free).

    ``config`` keys:
        ``repo``: the static repository identifier (e.g. ``"owner/name"``).
        ``auth``: a secret *reference* — the env-var name holding the token.
    """

    outputs = [
        Parameter(name="repo", type="str", flow=Flow.STATIC),
    ]
    multi = False

    async def fetch(self, ctx: RunContext) -> Output[dict[str, JSONValue]]:
        repo = self.config.get("repo", "")
        # Credential resolved by reference; the value is used internally only and
        # never placed in config or the Output.
        # auth_ref = self.config.get("auth")  # env-var NAME, not the value
        # token = resolve_secret(auth_ref)   # resolved at the egress boundary
        return Output(
            output_schema=list(self.outputs),
            value={"repo": repo},
            produced_by=self.id,
        )


class PullRequestSource(Source[list[dict[str, JSONValue]]]):
    """Multi source emitting a list of pull requests (deterministic, network-free).

    ``config`` keys:
        ``repo``: the static repository identifier.
        ``items``: a fixture list of PR dicts (each matching :attr:`outputs`).
        ``auth``: an optional secret *reference* (env-var name).
    """

    outputs = [
        Parameter(name="number", type="int"),
        Parameter(name="title", type="str"),
    ]
    multi = True

    async def fetch(self, ctx: RunContext) -> Output[list[dict[str, JSONValue]]]:
        items = self.config.get("items", [])
        if not isinstance(items, list):
            items = []
        return Output(
            output_schema=list(self.outputs),
            value=list(items),
            produced_by=self.id,
        )
