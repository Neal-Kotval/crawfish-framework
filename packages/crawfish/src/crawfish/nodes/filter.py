"""Filter — a node that routes/narrows a list Output (CRA-105).

A ``Filter`` takes an :class:`~crawfish.output.Output` whose value is a list and
emits a *fresh* Output containing only the items that satisfy a predicate, with
order preserved. Because ``Output`` is frozen, a Filter never mutates its input:
it always derives a new Output via :meth:`~crawfish.output.Output.derive`, leaving
the upstream value intact for audit.

Decision (CRA-105): Filter is a **first-class Node** — a pure, synchronous
transform with no side effects — rather than sugar over some other primitive.
This keeps it inspectable in the pipeline graph (it carries an id/name/kind) and
composable: the Output of one Filter feeds straight into the next.

The predicate factories (:func:`title_contains`, :func:`field_equals`,
:func:`field_matches`, :func:`limit`) return ready-to-use ``Filter`` instances for
the common dict-item cases. ``limit`` is special: a per-item predicate cannot
count, so it is a small :class:`Filter` subclass that slices instead of filtering.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Generic

from crawfish.core.ids import new_id
from crawfish.core.types import JSONValue, Node, NodeKind, T
from crawfish.output import Output

__all__ = [
    "Filter",
    "title_contains",
    "field_equals",
    "field_matches",
    "limit",
]


class Filter(Node, Generic[T]):
    """A pure, synchronous node that narrows a list Output by a predicate.

    The predicate is applied per item; matching items are kept in their original
    order. The input Output is left unchanged (it is frozen); :meth:`apply`
    returns a freshly derived Output with a new id.
    """

    def __init__(self, predicate: Callable[[T], bool], name: str = "filter") -> None:
        self.id = new_id()
        self.name = name
        self.kind = NodeKind.FILTER
        self.predicate = predicate

    def apply(self, inp: Output[list[T]]) -> Output[list[T]]:
        """Return a fresh Output keeping only items that satisfy the predicate."""
        kept = [item for item in inp.value if self.predicate(item)]
        return inp.derive(value=kept, produced_by=self.id)


class _Limit(Filter[T]):
    """A :class:`Filter` that keeps the first ``n`` items (a list slice).

    A per-item predicate cannot count, so ``limit`` overrides :meth:`apply` to
    slice. The stored predicate accepts everything, keeping the ``Filter``
    contract intact for any caller that inspects it.
    """

    def __init__(self, n: int, name: str = "limit") -> None:
        super().__init__(lambda _item: True, name=name)
        self.n = n

    def apply(self, inp: Output[list[T]]) -> Output[list[T]]:
        """Return a fresh Output keeping at most the first ``n`` items."""
        kept = list(inp.value[: self.n])
        return inp.derive(value=kept, produced_by=self.id)


def title_contains(needle: str, name: str = "title_contains") -> Filter[JSONValue]:
    """Keep dict items whose ``"title"`` field contains ``needle``."""

    def predicate(item: JSONValue) -> bool:
        title = item.get("title", "")
        return isinstance(title, str) and needle in title

    return Filter(predicate, name=name)


def field_equals(field: str, value: JSONValue, name: str = "field_equals") -> Filter[JSONValue]:
    """Keep dict items whose ``field`` equals ``value``."""

    def predicate(item: JSONValue) -> bool:
        return bool(item.get(field) == value)

    return Filter(predicate, name=name)


def field_matches(field: str, pattern: str, name: str = "field_matches") -> Filter[JSONValue]:
    """Keep dict items whose ``field`` (as a string) matches ``pattern`` (regex search)."""
    compiled = re.compile(pattern)

    def predicate(item: JSONValue) -> bool:
        candidate = item.get(field)
        return isinstance(candidate, str) and compiled.search(candidate) is not None

    return Filter(predicate, name=name)


def limit(n: int, name: str = "limit") -> Filter[JSONValue]:
    """Keep the first ``n`` items (a list slice, not a per-item test)."""
    return _Limit(n, name=name)
