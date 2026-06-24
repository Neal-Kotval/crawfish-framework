"""Confidence extraction & calibration-derived abstention thresholds.

The home of the *confidence* vocabulary the tameness layer keys off. Two pure,
deterministic pieces:

* :func:`extract_confidence` — read a ``[0,1]`` self-reported confidence out of an
  :class:`~crawfish.output.Output` (a declared ``confidence`` field, or a numeric token
  fallback). This is the signal ``cw.calibrate`` measures and the signal an
  ``EscalatingRuntime`` / abstention policy acts on.
* :func:`abstention_threshold` — turn a calibration **reliability curve** (confidence vs.
  observed accuracy, the bins :func:`crawfish.metrics.calibrate` already computes) into the
  confidence below which acting is unsafe. This replaces the historical *guessed constant*
  (the old ``EscalatingRuntime`` threshold) with an **evidence-derived** value: the lowest
  confidence at which observed accuracy still clears a stated ``target``.

No model call, no I/O, no wall clock, no global RNG: a fluid/untrusted value is *measured*,
never trusted as an instruction, and the derived threshold is a function of recorded
measurements only. The threshold is data the gate consumes, not a free knob.
"""

from __future__ import annotations

import re

from crawfish.core.types import JSONValue
from crawfish.output import Output

__all__ = [
    "extract_confidence",
    "abstention_threshold",
    # Re-exported from crawfish.abstain (TS-4): abstention is the decline-rather-than-act
    # sibling of this module's confidence vocabulary. The implementation lives in
    # crawfish.abstain (which imports extract_confidence from here); we re-export at the
    # bottom of the file so the two read as one surface without a circular import.
    "abstain_below",
    "abstain_below_calibrated",
    "is_abstention",
    "Abstention",
]

_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")


def _coerce_unit(value: object) -> float | None:
    """Coerce ``value`` to a ``[0,1]`` float, or ``None`` if it isn't numeric."""
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        f = float(value)
    elif isinstance(value, str):
        m = _NUMBER_RE.search(value)
        if m is None:
            return None
        f = float(m.group())
    else:
        return None
    # Clamp into the unit interval — a self-reported confidence is a probability.
    return max(0.0, min(1.0, f))


def extract_confidence(output: Output[JSONValue], *, field: str = "confidence") -> float | None:
    """Read a ``[0,1]`` self-reported confidence from ``output``, or ``None`` if absent.

    Resolution order (deterministic, no model call):

    1. If the typed value is a mapping carrying ``field``, coerce that to ``[0,1]``.
    2. Otherwise, if the whole value is itself numeric, use it.
    3. Otherwise ``None`` — the run reported no confidence (the caller decides whether a
       missing confidence abstains or proceeds).

    The value is *measured*, never trusted as an instruction: a fluid Output's
    self-reported confidence is just a number to be calibrated against ground truth.
    """
    value = output.value
    if isinstance(value, dict):
        if field in value:
            return _coerce_unit(value[field])
        return None
    return _coerce_unit(value)


def abstention_threshold(
    bin_confidence: list[float],
    bin_accuracy: list[float],
    bin_count: list[int],
    *,
    target: float = 0.9,
    default: float = 1.0,
) -> float:
    """Derive the confidence below which acting is unsafe, from a reliability curve.

    Given a calibration curve as parallel per-bin lists — mean predicted ``confidence``,
    observed ``accuracy``, and population ``count`` — return the **lowest bin confidence
    at which observed accuracy still clears ``target``**, treating every lower-confidence
    bin as the abstain region. This is the evidence-derived replacement for the old
    guessed escalation constant: the threshold is *read off measurements*, not chosen.

    Semantics:

    * Bins are considered in ascending confidence order (sorted here, so caller order
      doesn't matter).
    * The threshold is the smallest bin confidence ``c`` such that *every* bin with
      confidence ``>= c`` meets ``accuracy >= target`` — i.e. the boundary above which the
      model is reliable. Acting is permitted at ``confidence >= threshold``.
    * Empty bins (``count == 0``) carry no evidence and are skipped.
    * If no confidence level is reliable (or there is no evidence), return ``default``
      (``1.0`` — abstain on everything; fail safe).

    Pure and deterministic: a function of the recorded curve only.
    """
    if not 0.0 <= target <= 1.0:
        raise ValueError(f"target must be in [0, 1], got {target!r}")
    if not (len(bin_confidence) == len(bin_accuracy) == len(bin_count)):
        raise ValueError("bin_confidence/bin_accuracy/bin_count must be equal length")

    # Evidence-bearing bins only, ascending by confidence.
    bins = sorted(
        (
            (conf, acc)
            for conf, acc, n in zip(bin_confidence, bin_accuracy, bin_count, strict=True)
            if n > 0
        ),
        key=lambda b: b[0],
    )
    if not bins:
        return default

    # Walk from the top: the threshold is the lowest confidence from which all
    # higher-or-equal bins remain reliable. The moment a bin dips below target, every
    # confidence at or below it is unsafe, so the boundary is the next bin up.
    threshold = default
    for conf, acc in reversed(bins):
        if acc >= target:
            threshold = conf
        else:
            break
    return threshold


# -- abstention (TS-4) ------------------------------------------------------
# Re-export the typed-abstention surface here so this module is the single home of the
# confidence vocabulary (measure → calibrate → escalate-or-abstain). The import is at the
# bottom, after extract_confidence is defined, so crawfish.abstain (which imports
# extract_confidence from this module) does not form a circular import.
from crawfish.abstain import (  # noqa: E402
    Abstention,
    abstain_below,
    abstain_below_calibrated,
    is_abstention,
)
