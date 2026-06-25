"""``craw code propose`` / ``apply`` / ``reject`` — the human approval & promotion gate.

UNFILED-GATE (RFC §12.2 "promotion/approval gate … reusing the secret-broker approval
queue, keyed on ``(component, sha)``, fail-closed" **unified** with §12.4 HITL "stage a
typed diff + cost estimate, human approves before anything consequential/``--live``;
reject → ``learn --rollback``"). A *skill* is a guideline an injected agent can be talked
out of (RFC §12.1); this gate is **enforcement**.

Two enforced layers, both in this file (one owner):

1. **Staging** — :class:`ApprovalLedger`, a Store-backed approval queue keyed on
   ``(component, candidate_sha)``. ``propose`` stages a typed, field-level
   :func:`~crawfish.agentdiff.diff` of the candidate vs the current frozen sha **plus** the
   honest cost interval (:mod:`crawfish.cost`) into a *pending* record. ``apply`` promotes
   the staged candidate **only if** a matching human approval is recorded — otherwise it
   fails closed (exit :data:`~crawfish.code.EXIT_NO_APPROVAL`). Before promoting it re-runs
   :func:`~crawfish.build.assert_build_safe` (SECURITY.md: "a generated artifact must pass
   the assembly gate to ship"). ``reject`` calls :meth:`LearningLoop.rollback` — a pure
   pointer move, **no model call**.

2. **The hook** (the hard backstop) — :func:`hook_decision` is the pure decision function the
   plugin's PreToolUse hook (``plugin/hooks/``) calls before any consequential
   ``craw … --live`` / sink Bash invocation. It checks the approval ledger for a matching
   approved ``(component, sha)`` **and** the budget-ceiling state (UNFILED-COST). On no
   approval or ``ceiling_reached`` it returns ``permissionDecision: "deny"``; a hard
   violation exits **2**, which hard-stops the tool call even under
   ``--dangerously-skip-permissions`` / bypassPermissions mode. The agent cannot talk its
   way past a hook the way it can past a skill.

**Why a Store-backed ledger and not the in-memory ``QueuedApprovalQueue``.**
:class:`crawfish.secrets.QueuedApprovalQueue` is the right *shape* (pending → out-of-band
``resolve`` → a decision keyed by identity, fail-closed default-deny) but its decision map
lives in process memory — it cannot survive the propose→human-approve→apply boundary, which
spans **separate CLI processes** (and a PreToolUse hook in a *third*). This module reuses
that exact contract over the :class:`~crawfish.store.base.Store` protocol so a decision
persists across processes and folds ``org_id`` into the key — the same fail-closed,
identity-keyed semantics, made durable.

**Fail-closed, by construction.** ``apply`` reads only a ``code_approval`` record whose
``id`` is ``f"{component}:{candidate_sha}"`` and whose stored ``component``/``sha`` match the
request — so an approval minted for sha *A* can never satisfy a request for sha *B* (replay),
and a forged ``approved: true`` smuggled into *ledger content* (an observer ``detail``, a run
field) is never read as an approval, because the gate consults **only** its own record kind,
never tainted surface text. Fluid data never chooses the approval target and never
auto-approves.
"""

from __future__ import annotations

import argparse
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from crawfish.code import (
    EXIT_OK,
    SCHEMA_VERSIONS,
    ErrorCode,
    emit_error,
    emit_json,
)

if TYPE_CHECKING:
    from crawfish.definition import Definition
    from crawfish.learning import VersionRecord
    from crawfish.store.base import Store

# Self-registering schema versions (the SCHEMA_VERSIONS table is the single source of truth;
# ``setdefault`` keeps this verb's entries additive without a cross-module edit — CRA-269).
SCHEMA_VERSIONS.setdefault("code.propose", (1, 0))  # type: ignore[attr-defined]
SCHEMA_VERSIONS.setdefault("code.apply", (1, 0))  # type: ignore[attr-defined]
SCHEMA_VERSIONS.setdefault("code.reject", (1, 0))  # type: ignore[attr-defined]

#: The Store ``kind`` the staged proposals live under (one per ``(component, sha)``).
PROPOSAL_KIND = "code_proposal"
#: The Store ``kind`` a recorded **human decision** lives under. The gate reads ONLY this
#: kind to decide ``apply`` — never tainted surface text — so an injected ``approved: true``
#: in ledger content cannot grant approval.
APPROVAL_KIND = "code_approval"


def _record_id(component: str, sha: str) -> str:
    """The identity key both the proposal and the approval are keyed on.

    Keying on ``(component, sha)`` is the anti-replay invariant: an approval minted for one
    candidate sha can never satisfy an apply of a different sha (its ``id`` would not match),
    and a different component's approval is in a different row.
    """
    return f"{component}:{sha}"


@dataclass(frozen=True)
class ProposalRecord:
    """A staged candidate awaiting human approval (the ``craw.code.propose.v1`` body).

    ``diff`` is the typed field-level change set (path/from/to) vs ``base_sha``; ``cost``
    is the honest interval (``total`` ≤ ``expected`` ≤ ``worst_case``). ``approval`` reflects
    the *current* decision state read back from the :data:`APPROVAL_KIND` row — ``pending``
    until a human records one.
    """

    component: str
    candidate_sha: str
    base_sha: str
    diff: list[dict[str, object]]
    cost: dict[str, float]
    approval: str  # pending | approved | rejected

    def to_body(self) -> dict[str, object]:
        return {
            "component": self.component,
            "candidate_sha": self.candidate_sha,
            "base_sha": self.base_sha,
            "diff": self.diff,
            "cost_estimate": self.cost,
            "approval": self.approval,
        }


class ApprovalLedger:
    """A Store-backed, fail-closed approval queue keyed on ``(component, candidate_sha)``.

    The durable analogue of :class:`crawfish.secrets.QueuedApprovalQueue`: a ``propose``
    enqueues a :class:`ProposalRecord`; an out-of-band human records a decision; until a
    matching ``approve`` decision exists, :meth:`is_approved` returns ``False`` (fail-closed).
    Every read/write carries ``org_id`` so a decision in org A never satisfies an apply in
    org B. The gate consults **only** these two record kinds — never tainted surface text.
    """

    def __init__(self, store: Store, *, org_id: str = "local") -> None:
        self._store = store
        self._org = org_id

    # -- staging ------------------------------------------------------------
    def stage(self, proposal: ProposalRecord) -> None:
        """Persist a pending proposal (idempotent on ``(component, sha)``)."""
        self._store.put_record(
            PROPOSAL_KIND,
            _record_id(proposal.component, proposal.candidate_sha),
            {
                "component": proposal.component,
                "candidate_sha": proposal.candidate_sha,
                "base_sha": proposal.base_sha,
                "diff": proposal.diff,
                "cost": proposal.cost,
                "staged_at": time.time(),
            },
            org_id=self._org,
        )

    def get_proposal(self, component: str, sha: str) -> dict[str, object] | None:
        """The staged proposal row for ``(component, sha)``, or ``None`` if never staged."""
        rec = self._store.get_record(PROPOSAL_KIND, _record_id(component, sha), org_id=self._org)
        return dict(rec) if rec is not None else None

    # -- the out-of-band human decision (NEVER fluid-reachable) -------------
    def record_decision(self, component: str, sha: str, *, approve: bool) -> None:
        """Record a **human** approve/reject decision for ``(component, sha)``.

        This is the operator/console entry point (the analogue of
        :meth:`QueuedApprovalQueue.resolve`). It is *not* a fluid-reachable CLI verb — fluid
        session data can never call it, so it cannot auto-approve. The decision row carries its
        own ``component``/``sha`` so :meth:`is_approved` can re-verify the identity match (an
        approval for sha A is structurally inapplicable to sha B).
        """
        self._store.put_record(
            APPROVAL_KIND,
            _record_id(component, sha),
            {
                "component": component,
                "sha": sha,
                "decision": "approve" if approve else "reject",
                "decided_at": time.time(),
            },
            org_id=self._org,
        )

    def is_approved(self, component: str, sha: str) -> bool:
        """``True`` iff a human ``approve`` decision is recorded for **this exact** identity.

        Fail-closed: absent the row, a non-``approve`` decision, or a stored
        ``component``/``sha`` that does not match the request (a replayed/forged row) → ``False``.
        """
        rec = self._store.get_record(APPROVAL_KIND, _record_id(component, sha), org_id=self._org)
        if rec is None:
            return False
        # Re-verify the identity the row claims matches what we asked for: a decision row can
        # only authorize the exact (component, sha) it was minted for — never a substitute.
        return (
            rec.get("decision") == "approve"
            and rec.get("component") == component
            and rec.get("sha") == sha
        )


# ===========================================================================
# propose / apply / reject — the CLI surface.
# ===========================================================================
def register(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    """Register ``craw code propose|apply|reject`` (self-registering, one owner)."""
    from crawfish.code.cli import add_common_args

    pp = subparsers.add_parser("propose", help="stage a typed diff + cost estimate for approval")
    pp.add_argument("component", help="component directory (e.g. definitions/triage)")
    add_common_args(pp)
    pp.set_defaults(func=_cmd_propose)

    pa = subparsers.add_parser(
        "apply", help="promote a staged candidate IFF approved (fail-closed)"
    )
    pa.add_argument("component", help="component directory")
    pa.add_argument("sha", help="the candidate content sha to promote")
    add_common_args(pa)
    pa.set_defaults(func=_cmd_apply)

    pr = subparsers.add_parser("reject", help="roll back a staged candidate (learn --rollback, $0)")
    pr.add_argument("component", help="component directory")
    pr.add_argument("sha", help="the candidate content sha to roll back")
    add_common_args(pr)
    pr.set_defaults(func=_cmd_reject)


def _project_dir_for(component: str) -> Path:
    """The project dir holding ``.crawfish/`` for a component path (the component dir itself).

    Mirrors ``describe``/``estimate``: a component is authored as a directory and its
    ``.crawfish/`` ledger lives under it.
    """
    return Path(component)


def _compile_candidate(component: str, *, store: Store, org_id: str) -> Definition:
    """Compile the on-disk component through the jailed loader (agent-authored ⇒ confined).

    The candidate is the component as it sits on disk; compiling it yields a frozen Definition
    whose ``content_sha`` is the candidate sha the proposal is keyed on.
    """
    from crawfish.definition.jailed import load_definition_jailed
    from crawfish.jail import SandboxPolicy

    compiled = load_definition_jailed(
        Path(component), store=store, org_id=org_id, policy=SandboxPolicy(kind="fake")
    )
    return compiled.definition


def stage_proposal(component: str, *, store: Store, org_id: str = "local") -> ProposalRecord:
    """Compile the candidate, diff it vs the active base, and stage a pending proposal.

    The base is the component's current active lineage version (``LearningLoop.active``) if one
    exists, else the empty base (a first proposal). The cost estimate is the pure
    :func:`~crawfish.cost.estimate_cost` band (no model call). Returns the staged
    :class:`ProposalRecord` with its live ``approval`` state.
    """
    from crawfish.agentdiff import diff
    from crawfish.cost import estimate_cost

    candidate = _compile_candidate(component, store=store, org_id=org_id)
    candidate_sha = candidate.content_sha()

    base = _active_base(component, store=store, org_id=org_id)
    field_changes: list[dict[str, object]] = []
    base_sha = ""
    if base is not None:
        d = diff(base, candidate)
        base_sha = d.sha_before
        field_changes = [
            {"path": c.path, "from": c.before, "to": c.after, "kind": c.kind.value}
            for c in d.changes
        ]

    est = estimate_cost(candidate, items=1)
    cost = {
        "total_usd": round(est.total_usd, 4),
        "expected_usd": round(est.expected_usd, 4),
        "worst_case_usd": round(est.worst_case_usd, 4),
    }

    ledger = ApprovalLedger(store, org_id=org_id)
    proposal = ProposalRecord(
        component=component,
        candidate_sha=candidate_sha,
        base_sha=base_sha,
        diff=field_changes,
        cost=cost,
        approval="pending",
    )
    ledger.stage(proposal)
    # Reflect any pre-existing decision (idempotent re-propose of an already-decided sha).
    approval = _approval_state(ledger, component, candidate_sha)
    return ProposalRecord(
        component=component,
        candidate_sha=candidate_sha,
        base_sha=base_sha,
        diff=field_changes,
        cost=cost,
        approval=approval,
    )


def _learning_loop(component: str, *, store: Store, org_id: str = "local") -> _Lineage:
    """The component's Store-backed version lineage (the pure-pointer half of ``LearningLoop``).

    Promotions/rollbacks ride the same ``learning:<component>`` record kind +
    :class:`~crawfish.learning.VersionRecord` format the full ``LearningLoop`` uses, so a
    ``craw code apply`` promotion is visible to the dashboard/``review`` lineage reads and the
    SEC-4 breaker — without constructing a Tuner the gate's pure pointer moves never touch.
    """
    return _Lineage(component, store=store, org_id=org_id)


def _active_base(component: str, *, store: Store, org_id: str) -> Definition | None:
    """The component's current active lineage Definition, or ``None`` if none recorded."""
    lineage = _learning_loop(component, store=store, org_id=org_id)
    active = lineage.active()
    return active.definition if active is not None else None


def _approval_state(ledger: ApprovalLedger, component: str, sha: str) -> str:
    """Map the recorded decision to the ``pending | approved | rejected`` projection."""
    rec = ledger._store.get_record(  # noqa: SLF001 — same-module read of the gate's own kind
        APPROVAL_KIND, _record_id(component, sha), org_id=ledger._org
    )
    if rec is None:
        return "pending"
    return "approved" if rec.get("decision") == "approve" else "rejected"


class _Lineage:
    """The component's Store-backed version lineage — the pure-pointer half of ``LearningLoop``.

    A component's promotions/rollbacks are pure pointer moves over the same
    ``learning:<component>`` record kind and :class:`~crawfish.learning.VersionRecord` format
    the :class:`~crawfish.learning.LearningLoop` uses, so a ``craw code apply`` promotion is
    visible to the dashboard/``review`` lineage reads and the SEC-4 breaker. We reuse this
    minimal Store shape rather than constructing a full ``LearningLoop`` (which requires a
    Benchmark + PromptMutator + Tuner only its *improve* path needs) — the lineage persistence
    and the rollback pointer move never touch the Tuner. :meth:`rollback` mirrors
    :meth:`LearningLoop.rollback` exactly: re-activate + reset the regression baseline + emit
    the SEC-4 audit event — a $0 move with **no model call**.
    """

    def __init__(self, name: str, *, store: Store, org_id: str = "local") -> None:
        self.name = name
        self.store = store
        self.org_id = org_id

    @property
    def _kind(self) -> str:
        return f"learning:{self.name}"

    def history(self) -> list[VersionRecord]:
        from crawfish.learning import VersionRecord

        return [
            VersionRecord.model_validate(r)
            for r in self.store.list_records(self._kind, org_id=self.org_id)
        ]

    def _get(self, sha: str) -> VersionRecord | None:
        from crawfish.learning import VersionRecord

        raw = self.store.get_record(self._kind, sha, org_id=self.org_id)
        return None if raw is None else VersionRecord.model_validate(raw)

    def _record(self, rec: VersionRecord) -> None:
        self.store.put_record(self._kind, rec.sha, rec.model_dump(mode="json"), org_id=self.org_id)

    def active(self) -> VersionRecord | None:
        for rec in self.history():
            if rec.active:
                return rec
        return None

    def set_active(self, sha: str) -> None:
        """Flip the active flag to ``sha`` (exactly one active version at a time)."""
        for rec in self.history():
            want = rec.sha == sha
            if rec.active != want:
                rec.active = want
                self._record(rec)

    def rollback(self, sha: str) -> bool:
        """Re-activate a prior recorded version — the $0 pointer move (mirrors LearningLoop).

        Returns ``True`` iff ``sha`` was in the lineage (re-activated + baseline reset + audit
        emitted); ``False`` if it was never recorded (a reject of a never-promoted candidate is
        still a recorded *decision*, just not a lineage move). **No model call** either way.
        """
        from crawfish.anomaly import emit_promotion_audit
        from crawfish.eval import save_baseline

        rec = self._get(sha)
        if rec is None:
            return False
        self.set_active(sha)
        save_baseline(self.store, self._kind, rec.scores, org_id=self.org_id)
        emit_promotion_audit(
            self.store,
            event="rolled_back",
            agent=self.name,
            candidate_sha=sha,
            baseline_sha=rec.parent_sha or "",
            run_id=f"rollback:{self.name}",
            org_id=self.org_id,
        )
        return True


def _cmd_propose(args: argparse.Namespace) -> int:
    """``craw code propose <component>`` — stage a typed diff + cost estimate for approval."""
    from crawfish.manage import store_for_dir

    org = getattr(args, "org", "local")
    project = _project_dir_for(args.component)
    if not project.is_dir():
        return emit_error(
            ErrorCode.NOT_FOUND,
            remediation=f"Component {args.component!r} was not found; pass a component directory.",
            detail={"component": args.component},
            as_json=getattr(args, "as_json", False),
        )
    (project / ".crawfish").mkdir(parents=True, exist_ok=True)
    store = store_for_dir(str(project))
    try:
        try:
            proposal = stage_proposal(args.component, store=store, org_id=org)
        except Exception:  # compile / jail failure → compile_error (never a traceback)
            return emit_error(
                ErrorCode.COMPILE_ERROR,
                remediation=f"Component {args.component!r} failed to compile; fix it and retry.",
                detail={"component": args.component},
                as_json=getattr(args, "as_json", False),
            )
        body = proposal.to_body()
        if getattr(args, "as_json", False):
            emit_json("code.propose", body, org=org)
        else:
            _print_proposal(body)
        return EXIT_OK
    finally:
        store.close()


def _cmd_apply(args: argparse.Namespace) -> int:
    """``craw code apply <component> <sha>`` — promote IFF approved; else fail closed.

    Fail-closed order: (1) the candidate must have been staged; (2) a human approval for this
    exact ``(component, sha)`` must exist (absent → ``no_approval``, the closed exit 4 with a
    granular ``detail.exit=7``); (3) the aggregate budget ceiling must not be ``ceiling_reached``
    (→ ``ceiling_reached``, exit 4 / ``detail.exit=8``); (4) the candidate must re-pass
    :func:`assert_build_safe` before promotion.
    """
    from crawfish.build import assert_build_safe
    from crawfish.code import CODE_EXIT
    from crawfish.manage import store_for_dir

    org = getattr(args, "org", "local")
    project = _project_dir_for(args.component)
    if not project.is_dir():
        return emit_error(
            ErrorCode.NOT_FOUND,
            remediation=f"Component {args.component!r} was not found; pass a component directory.",
            detail={"component": args.component},
            as_json=getattr(args, "as_json", False),
        )
    (project / ".crawfish").mkdir(parents=True, exist_ok=True)
    store = store_for_dir(str(project))
    try:
        ledger = ApprovalLedger(store, org_id=org)
        if ledger.get_proposal(args.component, args.sha) is None:
            return emit_error(
                ErrorCode.NOT_FOUND,
                remediation=(
                    f"No staged candidate {args.sha!r} for {args.component!r}; "
                    "run `craw code propose` first."
                ),
                detail={"component": args.component, "sha": args.sha},
                as_json=getattr(args, "as_json", False),
            )
        # (2) FAIL CLOSED: no matching human approval → reject (non-retryable, security).
        if not ledger.is_approved(args.component, args.sha):
            return emit_error(
                ErrorCode.NO_APPROVAL,
                remediation=(
                    "This change is not approved. A human must approve the staged "
                    f"(component, sha) before `craw code apply`. component={args.component!r}."
                ),
                detail={"component": args.component, "sha": args.sha, "exit": 7},
                as_json=getattr(args, "as_json", False),
            )
        # (3) The aggregate budget ceiling is the same signal the hook reads (UNFILED-COST).
        if _ceiling_reached(project, org_id=org):
            return emit_error(
                ErrorCode.CEILING_REACHED,
                remediation=(
                    "The project budget ceiling is reached; promotion is halted until spend "
                    "falls below the [budget] ceiling."
                ),
                detail={"component": args.component, "sha": args.sha, "exit": 8},
                as_json=getattr(args, "as_json", False),
            )
        # (4) The candidate must re-pass the assembly gate before it can ship (SECURITY.md).
        try:
            candidate = _compile_candidate(args.component, store=store, org_id=org)
            if candidate.content_sha() != args.sha:
                # The on-disk component changed since it was staged → the approval is for a
                # different artifact. Fail closed rather than promote an unapproved sha.
                return emit_error(
                    ErrorCode.NO_APPROVAL,
                    remediation=(
                        "The component changed since approval; re-propose and re-approve the "
                        "new sha."
                    ),
                    detail={
                        "component": args.component,
                        "approved_sha": args.sha,
                        "current_sha": candidate.content_sha(),
                    },
                    as_json=getattr(args, "as_json", False),
                )
            assert_build_safe([candidate])
        except Exception as exc:  # assembly-gate rejection → fluid_to_static_sink (security)
            from crawfish.alg3 import FluidToStaticSinkError

            code = (
                ErrorCode.FLUID_TO_STATIC_SINK
                if isinstance(exc, FluidToStaticSinkError)
                else ErrorCode.COMPILE_ERROR
            )
            return emit_error(
                code,
                remediation=(
                    "The approved candidate fails the assembly gate and cannot be promoted; "
                    "fix the wiring (no fluid input may reach a static-only sink)."
                ),
                detail={"component": args.component, "sha": args.sha},
                as_json=getattr(args, "as_json", False),
            )
        # Promote: record the candidate as the now-active lineage version.
        loop = _learning_loop(args.component, store=store, org_id=org)
        _promote(loop, candidate)
        body = {"component": args.component, "sha": args.sha, "result": "applied"}
        if getattr(args, "as_json", False):
            emit_json("code.apply", body, org=org)
        else:
            print(f"applied {args.component} @ {args.sha}")
        return EXIT_OK
    finally:
        # Defensive: an unmapped exit fell through; CODE_EXIT is referenced so a future
        # reorganization keeps the mapping import-bound.
        _ = CODE_EXIT
        store.close()


def _cmd_reject(args: argparse.Namespace) -> int:
    """``craw code reject <component> <sha>`` — roll back via ``LearningLoop.rollback`` ($0).

    Records a human ``reject`` decision (so a later ``apply`` of the same sha stays fail-closed)
    and, when the sha is in the lineage, re-activates the prior version with a pure pointer move
    — **no model call**.
    """
    from crawfish.manage import store_for_dir

    org = getattr(args, "org", "local")
    project = _project_dir_for(args.component)
    if not project.is_dir():
        return emit_error(
            ErrorCode.NOT_FOUND,
            remediation=f"Component {args.component!r} was not found; pass a component directory.",
            detail={"component": args.component},
            as_json=getattr(args, "as_json", False),
        )
    (project / ".crawfish").mkdir(parents=True, exist_ok=True)
    store = store_for_dir(str(project))
    try:
        ledger = ApprovalLedger(store, org_id=org)
        ledger.record_decision(args.component, args.sha, approve=False)
        loop = _learning_loop(args.component, store=store, org_id=org)
        # Pure $0 pointer move; False if the sha was never promoted into the lineage (a reject
        # of a never-promoted candidate is still a recorded *decision*, just not a lineage move).
        rolled_back = loop.rollback(args.sha)
        body = {
            "component": args.component,
            "sha": args.sha,
            "result": "rejected",
            "rolled_back": rolled_back,
        }
        if getattr(args, "as_json", False):
            emit_json("code.reject", body, org=org)
        else:
            print(f"rejected {args.component} @ {args.sha} (rolled_back={rolled_back})")
        return EXIT_OK
    finally:
        store.close()


def _promote(loop: _Lineage, candidate: Definition) -> None:
    """Record ``candidate`` as the now-active lineage version (a frozen ``VersionRecord``).

    Uses the loop's own lineage record format so the promotion is visible to
    ``dashboard``/``review`` lineage reads and the SEC-4 breaker. A re-promotion of an
    already-recorded sha just re-activates it.
    """
    from crawfish.learning import VersionRecord

    sha = candidate.content_sha()
    existing = loop._get(sha)  # noqa: SLF001 — same-family lineage write
    if existing is None:
        active = loop.active()
        loop._record(  # noqa: SLF001
            VersionRecord(
                agent=loop.name,
                sha=sha,
                version=str(candidate.version),
                definition=candidate,
                scores={},
                role="candidate",
                parent_sha=(active.sha if active is not None else None),
                active=False,
            )
        )
    loop.set_active(sha)


# ===========================================================================
# The PreToolUse hook decision (pure; the plugin hook script calls this).
# ===========================================================================
@dataclass(frozen=True)
class HookDecision:
    """The PreToolUse decision the plugin hook emits (deny / ask / allow).

    ``hard_violation`` drives the **exit-2** backstop: an un-approved consequential ``--live``
    call (or a ``ceiling_reached`` state) is a hard stop that overrides an ``allow`` rule and
    bypassPermissions mode. A non-consequential command is ``allow`` (the hook is a backstop,
    not a blanket block).
    """

    decision: str  # deny | ask | allow
    reason: str
    hard_violation: bool

    def to_payload(self) -> dict[str, object]:
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": self.decision,
                "permissionDecisionReason": self.reason,
            }
        }


#: A command is **consequential** (gate-relevant) iff it can fire a live sink / promotion.
#: ``--live`` is the canonical marker; a bare ``craw run``/``eval`` in eval mode is free and
#: not gated. The hook is conservative: it gates the live/consequential family only, so it is
#: a backstop, not a blanket block on every command.
_CONSEQUENTIAL_MARKERS = ("--live", "--dangerously")
_CONSEQUENTIAL_VERBS = ("apply",)


def _is_consequential(command: str) -> bool:
    """True iff a Bash ``command`` string is a consequential ``craw … --live`` / promotion.

    Keyed on stable static markers only (never on fluid argument *values*): the ``--live``
    flag, the ``--dangerously-*`` bypass family, or a ``craw code apply`` promotion. Fluid
    data in the command can never *clear* the gate — it can only ever look more consequential.
    """
    if "craw" not in command:
        return False
    if any(marker in command for marker in _CONSEQUENTIAL_MARKERS):
        return True
    return any(f" {verb} " in f" {command} " for verb in _CONSEQUENTIAL_VERBS)


def hook_decision(
    command: str,
    *,
    is_approved: bool,
    ceiling_reached: bool,
) -> HookDecision:
    """The pure PreToolUse decision over the approval-queue + cost state (testable offline).

    * A non-consequential command → ``allow`` (the hook never blocks free work).
    * ``ceiling_reached`` → ``deny`` + hard violation, **regardless of approval** (UNFILED-COST
      is the load-bearing halt — an injected agent must not spend past the ceiling).
    * A consequential command with **no** matching approval → ``deny`` + hard violation.
    * A consequential command, approved, under the ceiling → ``allow``.

    Fluid data never reaches this function as an instruction: ``command`` is matched on static
    markers only, and ``is_approved`` is computed by :meth:`ApprovalLedger.is_approved` over the
    gate's own record kind — never over tainted surface text.
    """
    if not _is_consequential(command):
        return HookDecision("allow", "non-consequential command; not gated.", False)
    if ceiling_reached:
        return HookDecision(
            "deny",
            "craw code: project budget ceiling reached; consequential --live calls are halted "
            "until spend falls below the [budget] ceiling.",
            True,
        )
    if not is_approved:
        return HookDecision(
            "deny",
            "craw code: consequential --live call requires an approved (component, sha); none "
            "staged/approved. Run `craw code propose` then have a human approve before apply.",
            True,
        )
    return HookDecision("allow", "approved (component, sha) under the budget ceiling.", False)


# ===========================================================================
# Shared helpers.
# ===========================================================================
def _ceiling_reached(project: Path, *, org_id: str) -> bool:
    """``True`` iff the org's aggregate spend has reached the ``[budget]`` ceiling.

    Reuses the M4 dashboard cost gauge (UNFILED-COST) so the gate and the dashboard read the
    *same* ``ceiling_reached`` signal — there is one source of truth for the halt. A missing
    ceiling is unbounded (never reached).
    """
    from crawfish.code.dashboard import build_data

    try:
        data = build_data(project, org_id=org_id)
    except Exception:  # noqa: BLE001 — a missing ledger is "no ceiling reached", not a crash
        return False
    return data.cost_gauge().state == "ceiling_reached"


def _print_proposal(body: dict[str, object]) -> None:
    """Human rendering of a staged proposal (the typed diff + cost band + approval state)."""
    print(
        f"{body['component']} @ {body['candidate_sha']} (base {body['base_sha']}) — "
        f"approval: {body['approval']}"
    )
    cost = body.get("cost_estimate", {})
    if isinstance(cost, dict):
        print(
            f"  cost: total={cost.get('total_usd')} expected={cost.get('expected_usd')} "
            f"worst_case={cost.get('worst_case_usd')}"
        )
    changes = body.get("diff", [])
    if isinstance(changes, list):
        for ch in changes:
            if isinstance(ch, dict):
                print(f"  {ch.get('path')}: {ch.get('from')!r} -> {ch.get('to')!r}")
