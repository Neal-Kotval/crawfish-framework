"""Router & Classifier — conditional routing / branching.

A :class:`Router` is a first-class :class:`~crawfish.core.types.Node` that sends an
incoming :class:`~crawfish.output.Output` down one of several labelled branches. The
choice is made by a :class:`Classifier`, which produces one *typed* label from an
explicit, closed set (``labels``) — always including a ``default`` (dead-letter)
label so every item is routable.

Two classifier flavours share that contract:

* a **predicate/built-in** classifier (:meth:`Classifier.from_predicates`): an ordered
  mapping of ``label -> predicate(value) -> bool``; :meth:`Classifier.classify` returns
  the first matching label, or ``default``. Pure and synchronous.
* a **Definition-backed** classifier (:meth:`Classifier.from_definition`): runs an agent
  team (via :class:`~crawfish.run.Run` / ``run_team`` over an
  :class:`~crawfish.runtime.base.AgentRuntime`) that emits a label as text; the text is
  normalised to one of the allowed labels (``default`` on no match) by
  :meth:`Classifier.classify_async`.

Wiring is checked at **assembly time**: a :class:`Router` whose classifier can emit a
label with no matching branch raises :class:`UnroutableLabelError`, so an unroutable
pipeline never reaches run time.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

from crawfish.core.context import RunContext
from crawfish.core.ids import new_id
from crawfish.core.types import JSONValue, Node, NodeKind
from crawfish.definition.types import Definition
from crawfish.output import Output
from crawfish.run import Run
from crawfish.runtime.base import AgentRuntime

__all__ = ["Classifier", "Router", "UnroutableLabelError"]


class UnroutableLabelError(ValueError):
    """Raised at assembly time when a classifier label has no matching branch."""


Predicate = Callable[[JSONValue], bool]


def _normalise(text: str, labels: list[str], default: str) -> str:
    """Map free-form agent text to one of ``labels`` (``default`` on no match).

    Matching is case-insensitive: a label is chosen if it appears as a whitespace-
    delimited token in ``text`` (so ``"the label is bug."`` -> ``"bug"``). Labels are
    tried in declared order, so earlier labels win on ambiguity.
    """
    tokens = {tok.strip(".,:;!?\"'()[]{}").lower() for tok in text.split()}
    for label in labels:
        if label.lower() in tokens:
            return label
    return default


class Classifier:
    """Produces one typed label for an :class:`Output` from a closed label set.

    Construct via :meth:`from_predicates` (pure/built-in) or :meth:`from_definition`
    (agent-backed). ``labels`` is the explicit, closed set of possible labels and always
    includes ``default``.
    """

    def __init__(
        self,
        *,
        labels: list[str],
        default: str,
        predicates: Mapping[str, Predicate] | None = None,
        definition: Definition | None = None,
        name: str = "classifier",
    ) -> None:
        if default not in labels:
            raise ValueError(f"default label {default!r} must be in labels {labels}")
        self.id = new_id()
        self.name = name
        self.labels = list(labels)
        self.default = default
        self._predicates = dict(predicates) if predicates is not None else None
        self._definition = definition

    # -- constructors -------------------------------------------------------
    @classmethod
    def from_predicates(
        cls,
        predicates: Mapping[str, Predicate],
        *,
        default: str,
        name: str = "classifier",
    ) -> Classifier:
        """Build a pure classifier from an ordered ``label -> predicate`` mapping.

        The closed label set is the mapping's keys plus ``default`` (preserving the
        mapping's insertion order). :meth:`classify` returns the first matching label.
        """
        labels = list(predicates)
        if default not in labels:
            labels.append(default)
        return cls(labels=labels, default=default, predicates=predicates, name=name)

    @classmethod
    def from_definition(
        cls,
        definition: Definition,
        *,
        labels: list[str],
        default: str,
        name: str = "classifier",
    ) -> Classifier:
        """Build an agent-backed classifier over an explicit ``labels`` set."""
        return cls(labels=labels, default=default, definition=definition, name=name)

    # -- classification -----------------------------------------------------
    def classify(self, output: Output[JSONValue]) -> str:
        """Return the first predicate-matched label, or ``default`` (pure path)."""
        if self._predicates is None:
            raise TypeError(
                "classify() requires a predicate classifier; "
                "use classify_async() for a Definition-backed classifier"
            )
        for label, predicate in self._predicates.items():
            if predicate(output.value):
                return label
        return self.default

    async def classify_async(
        self,
        output: Output[JSONValue],
        ctx: RunContext,
        runtime: AgentRuntime,
    ) -> str:
        """Run the agent team on ``output`` and normalise its text to a label.

        Predicate classifiers short-circuit to :meth:`classify` (no model call).
        """
        if self._definition is None:
            return self.classify(output)
        # The classifier deliberately over-binds (one item value into every slot to
        # satisfy presence) and reads the run's free text — so it skips input-type and
        # output-schema validation (CRA-172).
        run = Run(
            self._definition,
            self._bind_inputs(output),
            validate_input_types=False,
            validate_output_schema=False,
        )
        result = await run.execute(ctx, runtime)
        return _normalise(str(result.value), self.labels, self.default)

    def _bind_inputs(self, output: Output[JSONValue]) -> dict[str, JSONValue]:
        """Bind ``output`` into the Definition's input slots for classification.

        The item under test is bound to every required slot (validation passes without
        forcing the caller to know the Definition's port names); the raw value is also
        offered under ``"item"`` for prompts that expect it.
        """
        assert self._definition is not None
        bound: dict[str, JSONValue] = {"item": output.value}
        for param in self._definition.inputs:
            if param.required and param.default is None:
                bound[param.name] = output.value
        return bound


class Router(Node):
    """A node that routes an Output down one labelled branch chosen by a Classifier.

    ``branches`` maps every classifier label (including ``default``, the dead-letter
    branch) to a downstream :class:`Node`. Construction fails with
    :class:`UnroutableLabelError` if any classifier label is uncovered (assembly-time
    check) — the routing graph is total before it ever runs.
    """

    def __init__(
        self,
        branches: Mapping[str, Node],
        classifier: Classifier,
        name: str = "router",
    ) -> None:
        uncovered = [label for label in classifier.labels if label not in branches]
        if uncovered:
            raise UnroutableLabelError(
                f"classifier labels {uncovered} have no matching router branch; "
                f"branches cover {sorted(branches)}"
            )
        if classifier.default not in branches:
            raise UnroutableLabelError(
                f"default branch {classifier.default!r} is not in branches {sorted(branches)}"
            )
        self.id = new_id()
        self.name = name
        self.kind = NodeKind.ROUTER
        self.branches: dict[str, Node] = dict(branches)
        self.classifier = classifier

    def route(self, output: Output[JSONValue]) -> tuple[str, Node]:
        """Classify ``output`` (pure path) and return the chosen ``(label, branch)``."""
        label = self.classifier.classify(output)
        return label, self.branches[label]

    async def route_async(
        self,
        output: Output[JSONValue],
        ctx: RunContext,
        runtime: AgentRuntime,
    ) -> tuple[str, Node]:
        """Classify ``output`` via the agent team and return ``(label, branch)``."""
        label = await self.classifier.classify_async(output, ctx, runtime)
        return label, self.branches[label]
