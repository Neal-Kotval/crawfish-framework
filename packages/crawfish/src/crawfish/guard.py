"""House-guard — learned-then-distilled deterministic guards (TS-7 / R4).

The deepest expression of the thesis: a program *accretes its own deterministic
invariants*. Quality is learned stochastically (the model proposes a candidate
rule from a mined corpus of corrections), then **distilled** into a pure,
side-effect-free predicate over an :class:`~crawfish.output.Output`, which only
**earns** the right to enforce after clearing an absolute precision-and-coverage
bar against trusted ground truth. Once earned, the guard is a deterministic
predicate (no model call) that blocks a disallowed output.

This mirrors three precedents:

* The :class:`~crawfish.verifier.Verifier` *earn-the-right-to-gate* discipline
  (``verifier.py``): a consequential authority (here, a guard that blocks a Sink)
  must be **measured** before it is granted authority, and **fails closed** when
  un-measured — it stays in :attr:`GuardStage.WARN`/``SHADOW`` and cannot block.
* The F-3 absolute-precision gate (``eval.precision_gate``): admission is an
  absolute decision-quality bar, not a relative regression — but here we widen it
  to a **joint** criterion (a precision *lower bound* AND a *coverage* floor), so a
  99%-precision / 2%-coverage rule cannot earn the right to block.
* The F-4 corpus (``eval.GoldenSet.from_corrections``): the ground truth a blocking
  guard is validated against is mined from *trusted, untainted* corrections — the
  provenance/taint gate in ``from_corrections`` is the corpus-poisoning control
  (Gap S4); a fluid-derived correction can never become a guard's ground truth.

**The one stochastic leaf.** Only :func:`propose_rule` calls the model. Its output
is FLUID and is parsed *as data* into a fixed, total, side-effect-free expression
grammar (:class:`Predicate`) — comparisons, set membership, numeric bounds, and
boolean combinators over typed Output fields, evaluated by an interpreter that
**never** uses ``eval``/``exec``. The proposer can never *widen* the grammar: an
emission that does not parse into the closed grammar is rejected, it does not
extend it. The distilled AST becomes STATIC (trusted, enforceable) only **after**
it clears the precision gate.

**Determinism.** The proposal is the isolated leaf (replays via cassette under a
mock/replay runtime); the distilled predicate is pure (same input ⇒ same ``0``/``1``,
zero model calls, replays identically); enforcement is eval-gated and reversible;
each synthesized validation mints a new content sha (it never edits a frozen prior
rule). Security: the guard is a consequential authority — fluid never sets the
guard, and taint propagates from any fluid input into the certificate's lineage.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field

from crawfish.core.context import RunContext
from crawfish.core.ids import new_id
from crawfish.core.types import JSONValue
from crawfish.definition.types import Definition
from crawfish.eval import GoldenSet
from crawfish.experiment import normal_ppf
from crawfish.metrics import Metric
from crawfish.output import Output
from crawfish.run import Run
from crawfish.runtime.base import AgentRuntime
from crawfish.validation import canonicalize

__all__ = [
    "GuardStage",
    "GuardGrammarError",
    "GuardNotEarned",
    "Predicate",
    "Comparison",
    "SetMembership",
    "NumericBound",
    "BoolCombination",
    "Always",
    "PredicateMetric",
    "Interval",
    "GuardCertificate",
    "HouseGuard",
    "wilson_lower_bound",
    "proportion_ci",
    "propose_rule",
    "distill",
    "synthesize_guard",
]


# ===========================================================================
# Lifecycle + errors
# ===========================================================================
class GuardStage(str, Enum):
    """The shadow→warn→block lifecycle of a guard's enforcement authority.

    A guard earns authority only by clearing the JOINT precision-and-coverage bar
    (see :func:`synthesize_guard`). Below the bar it stays in ``SHADOW``/``WARN`` and
    **cannot** block a Sink; only a guard that clears the bar reaches ``BLOCK``.
    """

    SHADOW = "shadow"  # observed only — predicate evaluated, never enforced
    WARN = "warn"  # surfaced as a warning, still cannot block an output
    BLOCK = "block"  # earned: may block a disallowed output (as consequential as a Sink)


class GuardGrammarError(ValueError):
    """A proposal could not be distilled into the closed predicate grammar.

    Raised by :func:`distill` when a FLUID proposer emission references an unknown
    operator, a non-typed field path, or a malformed term. The grammar is **fixed**:
    a proposal cannot *widen* it — an out-of-grammar proposal is rejected, never
    admitted as a new operator. This is the SECURITY-minor predicate-grammar control.
    """


class GuardNotEarned(Exception):
    """A guard was asked to enforce without clearing the joint precision/coverage bar.

    Raised by :func:`synthesize_guard` when the guard has **no corpus** (no trusted
    corrections to validate against) or when it fails the joint criterion — the gate
    **fails closed**: an un-validated guard stays in ``warn`` and is never granted
    authority to block by default. Mirrors :class:`~crawfish.eval.VerifierNotGated`.
    """


# ===========================================================================
# The closed predicate grammar (fixed, total, side-effect-free)
# ===========================================================================
# Every node is a frozen pydantic model carrying a ``kind`` discriminator. The set
# of node kinds is CLOSED: the distiller maps a proposal onto exactly these, and the
# interpreter (`Predicate.matches`) is total over them. There is NO `eval`/`exec`
# anywhere — the predicate is data, evaluated by a switch over the closed kinds.
#
# A predicate answers ONE question: "is this output DISALLOWED?" (``matches`` →
# True means *block*). Field paths are dotted; a missing/typed-mismatched field
# evaluates to a benign False (the predicate is total — it never raises on data).

_CMP_OPS = frozenset({"==", "!=", "<", "<=", ">", ">="})


def _resolve_field(value: JSONValue, field: str | None) -> JSONValue:
    """Resolve a dotted ``field`` path within a typed value; ``None`` if absent.

    Pure and total: an absent key or a non-mapping along the path yields ``None``
    rather than raising — the predicate grammar is *total* over arbitrary Output
    values (the interpreter never throws on data it is fed)."""
    if field is None:
        return value
    cur: JSONValue = value
    for part in field.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur


def _as_number(value: JSONValue) -> float | None:
    """View a JSON value as a float for numeric terms (``bool`` excluded)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


class Comparison(BaseModel):
    """``field OP literal`` over a typed Output field (canonical equality).

    ``op`` is one of ``== != < <= > >=``. Ordering operators apply only to numerics
    (a non-numeric side makes them ``False``); equality is canonical (records key-
    sorted) so ``{"a":1,"b":2}`` matches ``{"b":2,"a":1}``.
    """

    kind: Literal["comparison"] = "comparison"
    field: str | None = None
    op: str
    literal: JSONValue = None

    model_config = {"frozen": True}

    def matches(self, value: JSONValue) -> bool:
        if self.op not in _CMP_OPS:
            return False
        actual = _resolve_field(value, self.field)
        if self.op in ("==", "!="):
            equal = canonicalize(actual) == canonicalize(self.literal)
            return equal if self.op == "==" else not equal
        left = _as_number(actual)
        right = _as_number(self.literal)
        if left is None or right is None:
            return False
        if self.op == "<":
            return left < right
        if self.op == "<=":
            return left <= right
        if self.op == ">":
            return left > right
        return left >= right  # ">="


class SetMembership(BaseModel):
    """``field IN members`` (or ``NOT IN`` when ``negate``) — order-free membership.

    Members are compared by canonical JSON, so nested records/order do not matter.
    """

    kind: Literal["set_membership"] = "set_membership"
    field: str | None = None
    members: list[JSONValue] = Field(default_factory=list)
    negate: bool = False

    model_config = {"frozen": True}

    def matches(self, value: JSONValue) -> bool:
        actual = canonicalize(_resolve_field(value, self.field))
        member_set = {json.dumps(canonicalize(m), sort_keys=True) for m in self.members}
        present = json.dumps(actual, sort_keys=True) in member_set
        return (not present) if self.negate else present


class NumericBound(BaseModel):
    """``lo <= field <= hi`` numeric range (either bound optional, inclusive).

    A non-numeric/absent field is ``False`` (out of every range). With both bounds
    ``None`` the term is vacuously ``False`` (it bounds nothing).
    """

    kind: Literal["numeric_bound"] = "numeric_bound"
    field: str | None = None
    lo: float | None = None
    hi: float | None = None

    model_config = {"frozen": True}

    def matches(self, value: JSONValue) -> bool:
        if self.lo is None and self.hi is None:
            return False
        num = _as_number(_resolve_field(value, self.field))
        if num is None:
            return False
        if self.lo is not None and num < self.lo:
            return False
        if self.hi is not None and num > self.hi:
            return False
        return True


class BoolCombination(BaseModel):
    """``AND``/``OR`` of sub-predicates (``NOT`` is a one-term combination).

    The single recursive node. ``op`` is ``"and"``/``"or"``/``"not"``; ``"not"``
    requires exactly one term. Empty ``and`` is ``True`` (vacuous), empty ``or`` is
    ``False`` — the standard identities, keeping the interpreter total.
    """

    kind: Literal["bool_combination"] = "bool_combination"
    op: Literal["and", "or", "not"]
    terms: list[Predicate] = Field(default_factory=list)

    model_config = {"frozen": True}

    def matches(self, value: JSONValue) -> bool:
        if self.op == "not":
            if len(self.terms) != 1:
                return False
            return not self.terms[0].matches(value)
        if self.op == "and":
            return all(t.matches(value) for t in self.terms)
        return any(t.matches(value) for t in self.terms)


class Always(BaseModel):
    """The constant predicate (``value`` is its fixed truth). The grammar's unit.

    ``Always(value=False)`` is the safe identity a fail-closed distillation falls back
    to: a guard that blocks nothing.
    """

    kind: Literal["always"] = "always"
    value: bool = False

    model_config = {"frozen": True}

    def matches(self, value: JSONValue) -> bool:
        return self.value


# A predicate is exactly one of the closed node kinds. The union is the WHOLE
# grammar — there is no other way to express a guard, and the distiller can only
# produce these. `BoolCombination.terms` references this same union (recursive).
Predicate = Comparison | SetMembership | NumericBound | BoolCombination | Always

# Pydantic v2 needs the forward ref in `BoolCombination.terms` resolved now that
# `Predicate` is bound.
BoolCombination.model_rebuild()


def _predicate_content_sha(predicate: Predicate) -> str:
    """Stable hex SHA-256 over a predicate's canonical JSON (pure, no I/O).

    Two structurally-equal predicates hash equal; any change to the AST mints a new
    sha (the immutable-versioning discipline — a synthesized validation never edits a
    frozen prior rule, it creates a new one)."""
    payload = predicate.model_dump(mode="json")
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


# ===========================================================================
# Predicate as a Metric (the distilled, pure scoring leaf)
# ===========================================================================
class PredicateMetric(Metric):
    """A distilled :class:`Predicate` exposed as a pure :class:`~crawfish.metrics.Metric`.

    ``evaluate`` returns ``1.0`` when the predicate matches (the output is
    *disallowed*) and ``0.0`` otherwise — zero model calls, same input ⇒ same score.
    This is the bridge that lets a learned-then-distilled guard plug into the existing
    metric/rubric machinery as an ordinary coded signal.
    """

    def __init__(self, predicate: Predicate, *, name: str | None = None) -> None:
        self.predicate = predicate
        self.name = name or f"guard[{_predicate_content_sha(predicate)}]"

    def evaluate(self, output: Output[JSONValue]) -> float:
        return 1.0 if self.predicate.matches(output.value) else 0.0


# ===========================================================================
# Statistics — precision lower bound + coverage CI (stdlib-only, deterministic)
# ===========================================================================
class Interval(BaseModel):
    """A point estimate with a two-sided confidence interval ``[lo, hi]`` (frozen)."""

    model_config = {"frozen": True}

    point: float
    lo: float
    hi: float


def wilson_lower_bound(successes: int, n: int, *, alpha: float = 0.05) -> float:
    """Wilson score **lower** bound for a binomial proportion ``successes / n``.

    The honest small-sample lower confidence bound on precision: unlike the naive
    ``TP/(TP+FP)`` point estimate, it does not certify a high precision off a handful
    of decisions (3/3 has a Wilson lower bound well under 1.0). ``n == 0`` ⇒ ``0.0``
    (no evidence ⇒ the bar cannot be cleared — fail-closed arithmetic).

    Deterministic and pure: closed-form over :func:`crawfish.experiment.normal_ppf`
    (the F-8 statistical substrate), stdlib-only (no numpy/scipy).
    """
    if n <= 0:
        return 0.0
    z = normal_ppf(1.0 - alpha / 2.0)
    phat = successes / n
    denom = 1.0 + z * z / n
    centre = phat + z * z / (2.0 * n)
    margin = z * math.sqrt((phat * (1.0 - phat) + z * z / (4.0 * n)) / n)
    return max(0.0, (centre - margin) / denom)


def proportion_ci(successes: int, n: int, *, alpha: float = 0.05) -> Interval:
    """Two-sided Wilson score interval for a proportion (point + ``[lo, hi]``).

    Used for the guard's **coverage** (the fraction of disallowed ground-truth cases
    the predicate fires on). ``n == 0`` ⇒ a degenerate ``Interval(0, 0, 0)``."""
    if n <= 0:
        return Interval(point=0.0, lo=0.0, hi=0.0)
    z = normal_ppf(1.0 - alpha / 2.0)
    phat = successes / n
    denom = 1.0 + z * z / n
    centre = (phat + z * z / (2.0 * n)) / denom
    margin = z * math.sqrt((phat * (1.0 - phat) + z * z / (4.0 * n)) / n) / denom
    return Interval(point=phat, lo=max(0.0, centre - margin), hi=min(1.0, centre + margin))


# ===========================================================================
# The certificate (the honest, joint earn-the-right-to-block record)
# ===========================================================================
class GuardCertificate(BaseModel):
    """The honest measurement that decides whether a guard may block (frozen).

    Reports **both** a precision lower bound **and** recall/coverage with CIs, so
    graduation gates on a JOINT criterion: a high-precision but near-zero-coverage
    rule (e.g. 99%-precision / 2%-coverage) cannot earn the right to block. Carries
    ``org_id`` (tenancy) and ``content_sha`` (the predicate's lineage key); ``tainted``
    propagates fluid lineage so a consequential consumer can refuse a fluid-derived
    certificate as trusted ground truth.

    Field contract:

    * ``precision_point`` / ``precision_lb`` — the naive ``TP/(TP+FP)`` and its Wilson
      *lower* bound; graduation reads the **lower bound**, never the optimistic point.
    * ``coverage`` — the coverage proportion + CI: ``fired-and-correct / total-disallowed``
      (a recall over the disallowed ground truth).
    * ``n_decisions`` / ``n_disallowed`` — the support behind precision / coverage.
    * ``precision_floor`` / ``min_coverage`` — the JOINT thresholds this guard was
      gated against (recorded for the audit / re-gate).
    * ``earned`` — the decision: ``precision_lb >= precision_floor`` AND
      ``coverage.lo >= min_coverage`` AND there was a corpus (fail-closed otherwise).
    """

    model_config = {"frozen": True}

    org_id: str
    content_sha: str
    precision_point: float
    precision_lb: float
    coverage: Interval
    n_decisions: int
    n_disallowed: int
    precision_floor: float
    min_coverage: float
    alpha: float
    earned: bool
    tainted: bool = False
    reason: str = ""


def _labelled_examples(case: object) -> list[tuple[JSONValue, bool]]:
    """Expand one mined correction into labelled ``(value, disallowed)`` decision points.

    A correction names a WRONG output (F-4): the case ``output`` carries the *produced*
    (disallowed) value, and the case ``label`` the *corrected/expected* (allowed) value.
    Validating a guard against BOTH classes is what makes precision **and** coverage
    honestly measurable: the guard should fire on the produced (disallowed) example and
    NOT on the corrected (allowed) one. A firing on an allowed example is a false
    positive (lowers precision); a non-firing on a disallowed example is a coverage miss.

    The produced example is always present; the corrected example is included only when a
    distinct ``label`` exists (an unlabelled correction contributes a disallowed point
    but no allowed one). Pure and deterministic."""
    produced = getattr(case, "output", None)
    expected = getattr(case, "label", None)
    examples: list[tuple[JSONValue, bool]] = [(produced, True)]
    if expected is not None and canonicalize(expected) != canonicalize(produced):
        examples.append((expected, False))
    return examples


# ===========================================================================
# (1) Mine corpus — re-exported convenience over F-4
# ===========================================================================
# `GoldenSet.from_corrections` (eval.py) is THE corpus miner; we import and reuse it
# rather than re-implement the provenance/taint gate. The guard never mines its own
# corpus — it always validates against a GoldenSet built by the trusted F-4 path.


# ===========================================================================
# (2) Propose — the ONE stochastic leaf
# ===========================================================================
async def propose_rule(
    definition: Definition,
    golden: GoldenSet,
    ctx: RunContext,
    runtime: AgentRuntime,
    *,
    max_examples: int = 20,
) -> Output[JSONValue]:
    """Ask the model to PROPOSE a candidate guard rule — the one stochastic leaf.

    Runs ``definition`` as a single leaf :class:`~crawfish.run.Run` over a sample of
    the corrections corpus (bound as FLUID data) and returns its raw
    :class:`~crawfish.output.Output`. The emission is FLUID model text: it is parsed
    *as data* by :func:`distill` against the closed grammar — it can never widen the
    grammar or set the guard directly. The model call is the only stochastic primitive
    (it replays via cassette under a mock/replay runtime).

    Taint propagates: a proposal over tainted corpus rows is itself tainted, so the
    downstream certificate is marked fluid-derived.
    """
    cases = golden.cases()[:max_examples]
    examples: list[JSONValue] = [
        {"produced": c.output, "expected": c.label, "inputs": c.inputs} for c in cases
    ]
    # The corpus is bound as fluid (untrusted) data for the proposer. Mirror the
    # Verifier's _bind_inputs discipline: offer it under "corrections" and also bind it
    # to every required slot so the proposer Definition's ports are satisfied without
    # the caller knowing their names.
    inputs: dict[str, JSONValue] = {"corrections": examples}
    for param in definition.inputs:
        if param.required and param.default is None:
            inputs[param.name] = examples
    run = Run(
        definition,
        inputs,
        validate_input_types=False,
        validate_output_schema=False,
    )
    result = await run.execute(ctx, runtime)
    return Output(
        output_schema=[],
        value=result.value,
        produced_by=definition.id,
        tainted=bool(result.tainted),
        lineage=golden.name,
    )


# ===========================================================================
# (3) Distill — parse the FLUID proposal into the closed grammar (no eval/exec)
# ===========================================================================
def distill(proposal: JSONValue) -> Predicate:
    """Distill a FLUID proposal into a pure :class:`Predicate` — never ``eval``/``exec``.

    The proposal (a JSON object, or a JSON string the proposer emitted) is parsed *as
    data* into the closed grammar. Each node names a ``kind`` from the fixed set
    (``comparison``/``set_membership``/``numeric_bound``/``bool_combination``/``always``);
    an unknown kind, operator, or malformed term raises :class:`GuardGrammarError` — the
    proposal **cannot widen the grammar**, only select within it.

    Pure and deterministic: same proposal ⇒ same predicate. The returned AST is the
    candidate STATIC artifact (it becomes trusted only after the precision gate).
    """
    parsed = _coerce_proposal(proposal)
    return _distill_node(parsed)


def _coerce_proposal(proposal: JSONValue) -> Mapping[str, JSONValue]:
    """View a proposal as a mapping (decoding a single JSON object string if needed)."""
    if isinstance(proposal, str):
        stripped = proposal.strip()
        try:
            decoded = json.loads(stripped)
        except (ValueError, TypeError) as exc:
            raise GuardGrammarError(f"proposal is not valid JSON: {exc}") from exc
        proposal = decoded
    if not isinstance(proposal, dict):
        raise GuardGrammarError(f"proposal must be a grammar object, got {type(proposal).__name__}")
    return proposal


def _distill_node(node: JSONValue) -> Predicate:
    """Recursively distill ONE grammar node. Total over the closed kind set."""
    if not isinstance(node, dict):
        raise GuardGrammarError(f"predicate node must be an object, got {type(node).__name__}")
    kind = node.get("kind")
    if kind == "always":
        return Always(value=bool(node.get("value", False)))
    if kind == "comparison":
        op = node.get("op")
        if not isinstance(op, str) or op not in _CMP_OPS:
            raise GuardGrammarError(f"comparison op must be one of {sorted(_CMP_OPS)}, got {op!r}")
        return Comparison(field=_opt_field(node.get("field")), op=op, literal=node.get("literal"))
    if kind == "set_membership":
        members = node.get("members")
        if not isinstance(members, list):
            raise GuardGrammarError("set_membership requires a 'members' list")
        return SetMembership(
            field=_opt_field(node.get("field")),
            members=list(members),
            negate=bool(node.get("negate", False)),
        )
    if kind == "numeric_bound":
        return NumericBound(
            field=_opt_field(node.get("field")),
            lo=_opt_number(node.get("lo")),
            hi=_opt_number(node.get("hi")),
        )
    if kind == "bool_combination":
        op = node.get("op")
        if op not in ("and", "or", "not"):
            raise GuardGrammarError(f"bool_combination op must be and/or/not, got {op!r}")
        raw_terms = node.get("terms")
        if not isinstance(raw_terms, list):
            raise GuardGrammarError("bool_combination requires a 'terms' list")
        if op == "not" and len(raw_terms) != 1:
            raise GuardGrammarError("bool_combination 'not' requires exactly one term")
        return BoolCombination(op=op, terms=[_distill_node(t) for t in raw_terms])
    raise GuardGrammarError(
        f"unknown predicate kind {kind!r} — the grammar is closed and cannot be widened"
    )


def _opt_field(value: JSONValue) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise GuardGrammarError(
            f"field must be a dotted string or null, got {type(value).__name__}"
        )
    return value


def _opt_number(value: JSONValue) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise GuardGrammarError(f"numeric bound must be a number or null, got {value!r}")
    return float(value)


# ===========================================================================
# (4) Earn — validate against the corpus on the JOINT criterion (fails closed)
# ===========================================================================
def synthesize_guard(
    predicate: Predicate,
    golden: GoldenSet,
    *,
    precision_floor: float,
    min_coverage: float,
    alpha: float = 0.05,
    org_id: str | None = None,
    tainted: bool = False,
) -> tuple[GuardCertificate, GuardStage]:
    """Validate a distilled ``predicate`` against ``golden`` on the JOINT criterion.

    Computes, against the trusted corrections corpus:

    * **precision** — of the cases the predicate fires on (predicts *disallowed*), the
      fraction whose ground truth is disallowed; reported as a Wilson **lower bound**
      (graduation reads the lower bound, never the optimistic point).
    * **coverage** — of the disallowed ground-truth cases, the fraction the predicate
      fires on, with a Wilson CI.

    Earns :attr:`GuardStage.BLOCK` **iff** ``precision_lb >= precision_floor`` AND
    ``coverage.lo >= min_coverage`` AND the corpus is non-empty. Otherwise the guard
    stays in :attr:`GuardStage.WARN` — the gate **fails closed**: a guard with no
    corpus (nothing to validate against) never blocks by default.

    Returns the honest :class:`GuardCertificate` and the earned stage. Pure and
    deterministic (no model call, no clock): the same predicate + corpus always yield
    the same certificate and the same ``content_sha`` (a fresh validation mints a new
    sha only when the predicate differs). Tenancy: ``org_id`` defaults to the golden
    set's org. Taint propagates into the certificate.
    """
    if not 0.0 <= precision_floor <= 1.0:
        raise ValueError(f"precision_floor must be in [0, 1], got {precision_floor!r}")
    if not 0.0 <= min_coverage <= 1.0:
        raise ValueError(f"min_coverage must be in [0, 1], got {min_coverage!r}")

    resolved_org = org_id if org_id is not None else getattr(golden, "_org", "local")
    content_sha = _predicate_content_sha(predicate)
    cases = golden.cases()

    # Expand each correction into labelled decision points (produced=disallowed,
    # corrected=allowed). tp: fires AND disallowed; fp: fires AND allowed.
    tp = fp = 0
    n_disallowed = 0
    fired_on_disallowed = 0
    for case in cases:
        for value, disallowed in _labelled_examples(case):
            fires = predicate.matches(value)
            if disallowed:
                n_disallowed += 1
                if fires:
                    fired_on_disallowed += 1
            if fires:
                if disallowed:
                    tp += 1
                else:
                    fp += 1

    n_decisions = tp + fp
    precision_point = tp / n_decisions if n_decisions else 0.0
    precision_lb = wilson_lower_bound(tp, n_decisions, alpha=alpha)
    coverage = proportion_ci(fired_on_disallowed, n_disallowed, alpha=alpha)

    # -- the JOINT, fail-closed criterion ----------------------------------
    has_corpus = len(cases) > 0 and n_decisions > 0
    precision_ok = precision_lb >= precision_floor
    coverage_ok = coverage.lo >= min_coverage
    earned = has_corpus and precision_ok and coverage_ok

    if not has_corpus:
        reason = "no corpus / no positive decisions — gate fails closed, stays in warn"
    elif not precision_ok:
        reason = (
            f"precision lower bound {precision_lb:.4g} below floor {precision_floor:.4g} — "
            f"stays in warn"
        )
    elif not coverage_ok:
        reason = (
            f"coverage lower bound {coverage.lo:.4g} below min {min_coverage:.4g} "
            f"(precision alone is insufficient) — stays in warn"
        )
    else:
        reason = (
            f"earned: precision_lb {precision_lb:.4g} >= {precision_floor:.4g} AND "
            f"coverage_lb {coverage.lo:.4g} >= {min_coverage:.4g}"
        )

    certificate = GuardCertificate(
        org_id=resolved_org,
        content_sha=content_sha,
        precision_point=precision_point,
        precision_lb=precision_lb,
        coverage=coverage,
        n_decisions=n_decisions,
        n_disallowed=n_disallowed,
        precision_floor=precision_floor,
        min_coverage=min_coverage,
        alpha=alpha,
        earned=earned,
        tainted=bool(tainted),
        reason=reason,
    )
    stage = GuardStage.BLOCK if earned else GuardStage.WARN
    return certificate, stage


# ===========================================================================
# The guard artifact
# ===========================================================================
class HouseGuard:
    """A learned-then-distilled deterministic guard — versioned, eval-gated, reversible.

    Holds the distilled :class:`Predicate`, the :class:`GuardCertificate` that earned
    (or did not earn) its enforcement authority, and a :class:`GuardStage`. A guard is
    built only through :meth:`synthesize` (which runs the joint precision/coverage gate)
    — a guard cannot self-promote to ``BLOCK``.

    Once earned, :meth:`blocks` is a pure deterministic predicate (no model call) that
    answers "is this output disallowed?" — but it only *enforces* (``can_block``) in
    stage ``BLOCK``. Content-hashed via the predicate sha (the lineage key); carries
    ``org_id``. Reversible: a promoted guard rolls back to any prior validated rule by
    re-synthesizing the earlier predicate (a fresh validation mints its own sha; it
    never edits a frozen prior rule).
    """

    def __init__(
        self,
        predicate: Predicate,
        certificate: GuardCertificate,
        stage: GuardStage,
        *,
        name: str = "house_guard",
    ) -> None:
        self.predicate = predicate
        self.certificate = certificate
        self.stage = stage
        self.name = name
        self.id = new_id()

    @property
    def content_sha(self) -> str:
        """The predicate's content sha — the lineage key (a new rule ⇒ a new sha)."""
        return self.certificate.content_sha

    @property
    def org_id(self) -> str:
        return self.certificate.org_id

    @property
    def can_block(self) -> bool:
        """Whether this guard may block an output. True only in stage ``BLOCK``."""
        return self.stage is GuardStage.BLOCK

    @property
    def tainted(self) -> bool:
        return self.certificate.tainted

    def matches(self, output: Output[JSONValue]) -> bool:
        """Pure predicate: True iff ``output`` is *disallowed* (no model call).

        Always available (shadow/warn/block) for observation; :meth:`blocks` adds the
        enforcement-authority gate on top.
        """
        return self.predicate.matches(output.value)

    def blocks(self, output: Output[JSONValue]) -> bool:
        """Whether this guard ENFORCES a block on ``output``.

        ``True`` only when the guard has earned authority (``can_block``) **and** the
        predicate matches. A guard in ``shadow``/``warn`` returns ``False`` here even
        when its predicate matches — it cannot stop a Sink until it earns the right.
        """
        return self.can_block and self.matches(output)

    def as_metric(self) -> PredicateMetric:
        """Expose the distilled predicate as a pure :class:`~crawfish.metrics.Metric`."""
        return PredicateMetric(self.predicate, name=f"{self.name}[{self.content_sha}]")

    @classmethod
    def synthesize(
        cls,
        predicate: Predicate,
        golden: GoldenSet,
        *,
        precision_floor: float,
        min_coverage: float,
        alpha: float = 0.05,
        org_id: str | None = None,
        tainted: bool = False,
        name: str = "house_guard",
    ) -> HouseGuard:
        """Validate ``predicate`` against ``golden`` and mint a guard at its earned stage.

        Runs :func:`synthesize_guard` (the joint, fail-closed gate) and wraps the result.
        A guard that does not clear the bar is returned in :attr:`GuardStage.WARN` (it
        carries its honest certificate but cannot block) — synthesis never raises on a
        below-bar guard; it fails *closed* by withholding authority. Use
        :meth:`require_earned` if the caller wants a hard failure instead.
        """
        certificate, stage = synthesize_guard(
            predicate,
            golden,
            precision_floor=precision_floor,
            min_coverage=min_coverage,
            alpha=alpha,
            org_id=org_id,
            tainted=tainted,
        )
        return cls(predicate, certificate, stage, name=name)

    def require_earned(self) -> HouseGuard:
        """Return ``self`` if earned; else raise :class:`GuardNotEarned` (fail-closed).

        For callers that want enforcement-or-nothing: a guard that did not clear the
        joint bar raises rather than silently degrading to a non-blocking guard."""
        if not self.can_block:
            raise GuardNotEarned(self.certificate.reason)
        return self
