"""Abstention — selective prediction as a typed Output discipline (TS-4).

Selective prediction (decline rather than hallucinate) is the formal frame for a
reliable agent, but until now the tameness layer could only *escalate* (re-run on a
stronger model, ``runtime/escalate.py``) — it could never **give up**. This module adds
the missing primitive: a first-class "I decline to answer" that is a typed Output value,
**not** an exception or a magic string.

Two pieces, both deterministic and pure over a recorded confidence:

* :class:`Abstention` — a frozen payload (``reason``, the *measured* ``confidence``, the
  ``threshold`` it fell under, and the producing run's ``tainted`` bit). It serialises to
  a JSON dict carrying a stable discriminator key, so an ``Output`` whose ``value`` is an
  Abstention is *routable*: a :class:`~crawfish.nodes.router.Router` predicate built from
  :func:`is_abstention` can branch ``Abstention → review_sink``.
* :func:`abstain_below` — a discipline/combinator (mirrors
  :func:`crawfish.runtime.escalate.confidence_below`) that turns a low-confidence
  :class:`~crawfish.output.Output` into one carrying an :class:`Abstention`. The
  confidence is **measured** from the (possibly fluid) Output via
  :func:`crawfish.escalate.extract_confidence` — never trusted as an instruction — and a
  *missing* confidence abstains (fail safe: declining is always allowed).

The threshold is meant to come from ``cw.calibrate``'s reliability curve
(:attr:`crawfish.metrics.CalibrationReport.abstention_threshold` /
:func:`crawfish.escalate.abstention_threshold`) — the confidence where observed accuracy
crosses target — never a guessed constant. :func:`abstain_below_calibrated` wires that
evidence-derived value straight in.

No model call, no I/O, no wall clock, no global RNG: the confidence is recorded data, the
abstain decision is a pure threshold over it, and the :class:`Abstention` is frozen and
carries taint forward.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from pydantic import BaseModel

from crawfish.core.types import JSONValue

# NOTE: ``extract_confidence`` is imported lazily inside ``discipline`` below.
# ``crawfish.escalate`` re-exports this module's abstention surface (single confidence
# home), so a top-level ``abstain → escalate`` edge would form an import cycle whenever
# ``abstain`` is imported first (e.g. from the package ``__init__``). The function-level
# import defers the edge to call time, after both modules are fully initialised.
from crawfish.output import Output

if TYPE_CHECKING:
    from crawfish.metrics import CalibrationReport

__all__ = [
    "Abstention",
    "ABSTENTION_MARKER",
    "is_abstention",
    "abstain_below",
    "abstain_below_calibrated",
]

# A stable discriminator key on the serialised Abstention dict. Routability hinges on a
# predicate being able to recognise an abstaining Output from its plain JSON ``value``
# alone (no Python type survives a persisted/replayed Output), so the marker travels in
# the value itself. Underscore-prefixed so it never collides with a model's own field.
ABSTENTION_MARKER = "_abstention"


class Abstention(BaseModel):
    """A typed "I decline to answer" — a first-class Output value, frozen.

    Carries the *measured* ``confidence`` (``None`` when the run reported none), the
    ``threshold`` it fell under, a human ``reason``, and the producing run's ``tainted``
    bit so taint propagates into the decline. Serialises (via :meth:`as_value`) to a JSON
    dict tagged with :data:`ABSTENTION_MARKER`, which is what makes an abstaining Output
    routable by an :func:`is_abstention` predicate.
    """

    model_config = {"frozen": True}

    reason: str
    confidence: float | None = None
    threshold: float | None = None
    tainted: bool = False
    field: str = "confidence"

    def as_value(self) -> dict[str, JSONValue]:
        """The JSON dict an abstaining ``Output`` carries as its ``value``.

        Tagged with :data:`ABSTENTION_MARKER` (``True``) so :func:`is_abstention`
        recognises it from the plain value after persist/replay.
        """
        return {
            ABSTENTION_MARKER: True,
            "reason": self.reason,
            "confidence": self.confidence,
            "threshold": self.threshold,
            "field": self.field,
        }

    @classmethod
    def from_value(cls, value: JSONValue, *, tainted: bool = False) -> Abstention | None:
        """Reconstruct an :class:`Abstention` from a tagged Output value, or ``None``.

        The inverse of :meth:`as_value`. ``None`` when ``value`` is not an abstention
        dict, so this doubles as a typed guard. ``tainted`` is supplied by the caller
        (it lives on the carrying :class:`~crawfish.output.Output`, not in the value).
        """
        if not is_abstention(value):
            return None
        assert isinstance(value, dict)  # is_abstention guarantees a tagged mapping
        return cls(
            reason=str(value.get("reason", "")),
            confidence=value.get("confidence"),
            threshold=value.get("threshold"),
            field=str(value.get("field", "confidence")),
            tainted=tainted,
        )


def is_abstention(value: JSONValue) -> bool:
    """``True`` iff ``value`` is a tagged :class:`Abstention` dict (a routable predicate).

    Pure and total over any JSON value — safe to hand to
    :meth:`crawfish.nodes.router.Classifier.from_predicates` so a ``Router`` can branch
    ``Abstention → review_sink``.
    """
    return isinstance(value, dict) and value.get(ABSTENTION_MARKER) is True


# A discipline: an Output transform that may replace its argument with an Abstention.
AbstainDiscipline = Callable[[Output[JSONValue]], Output[JSONValue]]


def abstain_below(
    threshold: float,
    *,
    field: str = "confidence",
    reason: str | None = None,
) -> AbstainDiscipline:
    """A discipline that turns a low-confidence Output into an :class:`Abstention`.

    Mirrors :func:`crawfish.runtime.escalate.confidence_below`, but it acts on a frozen
    :class:`~crawfish.output.Output` (not a raw ``RunResult``) and *declines* rather than
    escalating. The returned callable:

    * **measures** the confidence from the Output via
      :func:`crawfish.escalate.extract_confidence` — a fluid/untrusted self-report is
      just data, never an instruction;
    * returns a fresh Output carrying an :class:`Abstention` (via
      :meth:`Output.derive`, so **taint and lineage propagate**) when the confidence is
      below ``threshold`` **or absent** — a missing confidence abstains, because
      declining is the fail-safe action and is always allowed;
    * otherwise returns the input Output **unchanged** (confident enough to act).

    Deterministic: a pure threshold over a recorded number. Idempotent on an Output that
    already carries an Abstention (it has no readable confidence in ``field``, so it stays
    abstained rather than being re-wrapped).
    """

    def discipline(output: Output[JSONValue]) -> Output[JSONValue]:
        from crawfish.escalate import extract_confidence

        # An already-abstaining Output stays as-is (don't double-wrap).
        if is_abstention(output.value):
            return output

        confidence = extract_confidence(output, field=field)
        if confidence is not None and confidence >= threshold:
            return output  # confident enough to act — pass through untouched

        if confidence is None:
            why = reason or f"no readable {field!r} confidence; abstaining (fail safe)"
        else:
            why = reason or (f"measured {field} {confidence:.4g} below threshold {threshold:.4g}")
        abstention = Abstention(
            reason=why,
            confidence=confidence,
            threshold=threshold,
            field=field,
            tainted=output.tainted,
        )
        # derive() propagates taint + lineage from the producing Output.
        return output.derive(value=abstention.as_value(), produced_by=output.produced_by)

    return discipline


def abstain_below_calibrated(
    report: CalibrationReport,
    *,
    field: str = "confidence",
    reason: str | None = None,
) -> AbstainDiscipline:
    """:func:`abstain_below` wired to a calibration-derived threshold (the sound default).

    Reads :attr:`crawfish.metrics.CalibrationReport.abstention_threshold` — the confidence
    where observed accuracy crosses target, read off the reliability curve — instead of a
    guessed constant. On a mis-calibrated fixture this differs from any naive constant,
    which is the whole point (the issue's "raw constant is unsound" risk).
    """
    return abstain_below(report.abstention_threshold, field=field, reason=reason)
