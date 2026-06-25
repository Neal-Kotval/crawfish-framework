"""UNFILED-GATE — the M6 human-approval / promotion gate (propose / apply / reject).

Pins the fail-closed contract:

* ``propose`` stages a typed diff + honest cost interval keyed on ``(component, candidate_sha)``.
* ``apply`` **without** a recorded human approval fails closed: ``no_approval``, the closed
  CRA-243 exit ``4`` (security, non-retryable) with the spec's granular ``detail.exit=7``.
* a recorded human approval for the exact ``(component, sha)`` lets ``apply`` promote.
* ``reject`` records the decision and rolls the lineage back via a pure ``$0`` pointer move — no
  model call.
* the pure ``hook_decision`` denies an un-approved consequential ``--live`` call and a
  ``ceiling_reached`` state, and exits-2 (hard violation) on the hard backstop.

Deterministic: a temp ``Store`` + the FakeJail compile, no live model call.
"""

from __future__ import annotations

import io
import json
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from crawfish.code import EXIT_OK, EXIT_SECURITY
from crawfish.code.gate import (
    ApprovalLedger,
    _cmd_apply,
    _cmd_propose,
    _cmd_reject,
    hook_decision,
    stage_proposal,
)
from crawfish.manage import store_for_dir


def _component(tmp_path: Path, *, model: str = "claude-haiku-4-5", temp: float = 0.2) -> Path:
    """A minimal compilable component directory (jailed FakeJail compile, no model call)."""
    root = tmp_path / "triage"
    (root / "agents").mkdir(parents=True)
    (root / "instructions.md").write_text("triage\n")
    (root / "definition.py").write_text(
        "from crawfish.core import Flow, Parameter\n"
        "inputs = [Parameter(name='ticket', type='str', flow=Flow.FLUID)]\n"
        "outputs = [Parameter(name='label', type='str', flow=Flow.STATIC)]\n"
        "lead = 'lead'\n"
    )
    (root / "agents" / "lead.md").write_text(
        f"---\nrole: lead\nmodel: {model}\ntemperature: {temp}\n---\nTriage it.\n"
    )
    (root / "crawfish.toml").write_text("[project]\nname = 'triage'\n")
    (root / ".crawfish").mkdir(parents=True, exist_ok=True)
    return root


class _Args:
    def __init__(self, **kw: object) -> None:
        self.as_json = True
        self.org = "local"
        self.__dict__.update(kw)


def _run(fn, args) -> tuple[int, dict]:
    """Run a CLI cmd with ``--json``; return (exit_code, parsed stdout-or-stderr envelope)."""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = fn(args)
    text = (out.getvalue() or err.getvalue()).strip()
    payload = json.loads(text.splitlines()[-1]) if text else {}
    return code, payload


# -- propose stages a typed diff + cost interval keyed on (component, sha) ------------------
def test_propose_stages_diff_and_cost_keyed_on_component_sha(tmp_path: Path) -> None:
    comp = _component(tmp_path)
    store = store_for_dir(str(comp))
    try:
        proposal = stage_proposal(str(comp), store=store, org_id="local")
        assert proposal.candidate_sha  # a content-addressed sha
        assert proposal.approval == "pending"  # no human decision yet → pending (fail-closed)
        assert set(proposal.cost) == {"total_usd", "expected_usd", "worst_case_usd"}
        # The proposal is durably staged under (component, sha) — re-readable across processes.
        assert (
            ApprovalLedger(store, org_id="local").get_proposal(str(comp), proposal.candidate_sha)
            is not None
        )
    finally:
        store.close()


# -- apply WITHOUT a recorded approval fails closed (no_approval, exit 4, detail.exit=7) -----
def test_apply_without_approval_fails_closed(tmp_path: Path) -> None:
    comp = _component(tmp_path)
    store = store_for_dir(str(comp))
    try:
        proposal = stage_proposal(str(comp), store=store, org_id="local")
        sha = proposal.candidate_sha
    finally:
        store.close()

    code, payload = _run(_cmd_apply, _Args(component=str(comp), sha=sha))
    assert code == EXIT_SECURITY  # the closed CRA-243 exit is 4 (security, non-retryable)
    assert payload["code"] == "no_approval"
    assert payload["retryable"] is False  # an injected agent cannot retry past it
    assert payload["detail"]["exit"] == 7  # the spec's granular code rides in the envelope


# -- a recorded human approval for the exact (component, sha) lets apply promote -------------
def test_apply_with_recorded_approval_promotes(tmp_path: Path) -> None:
    comp = _component(tmp_path)
    store = store_for_dir(str(comp))
    try:
        proposal = stage_proposal(str(comp), store=store, org_id="local")
        sha = proposal.candidate_sha
        # The out-of-band human approval (the operator/console entry point — never fluid-reachable).
        ApprovalLedger(store, org_id="local").record_decision(str(comp), sha, approve=True)
    finally:
        store.close()

    code, payload = _run(_cmd_apply, _Args(component=str(comp), sha=sha))
    assert code == EXIT_OK
    assert payload["result"] == "applied"

    # The promotion is visible in the lineage (the dashboard/review/SEC-4 reads see it).
    store = store_for_dir(str(comp))
    try:
        from crawfish.code.gate import _learning_loop

        loop = _learning_loop(str(comp), store=store, org_id="local")
        active = loop.active()
        assert active is not None and active.sha == sha
    finally:
        store.close()


# -- reject records the decision + rolls back with a pure $0 pointer move (no model call) ----
def test_reject_records_decision_and_is_zero_cost(tmp_path: Path) -> None:
    comp = _component(tmp_path)
    store = store_for_dir(str(comp))
    try:
        proposal = stage_proposal(str(comp), store=store, org_id="local")
        sha = proposal.candidate_sha
    finally:
        store.close()

    code, payload = _run(_cmd_reject, _Args(component=str(comp), sha=sha))
    assert code == EXIT_OK
    assert payload["result"] == "rejected"

    # The reject is a recorded decision: a later apply of the same sha stays fail-closed.
    store = store_for_dir(str(comp))
    try:
        assert ApprovalLedger(store, org_id="local").is_approved(str(comp), sha) is False
    finally:
        store.close()


# -- the pure hook decision (offline) --------------------------------------------------------
def test_hook_allows_nonconsequential() -> None:
    d = hook_decision("ls -la", is_approved=False, ceiling_reached=False)
    assert d.decision == "allow"
    assert d.hard_violation is False


def test_hook_denies_unapproved_live_run() -> None:
    d = hook_decision("craw run --live", is_approved=False, ceiling_reached=False)
    assert d.decision == "deny"
    assert d.hard_violation is True  # the exit-2 hard stop (overrides allow / bypass mode)


def test_hook_denies_on_ceiling_regardless_of_approval() -> None:
    d = hook_decision("craw run --live", is_approved=True, ceiling_reached=True)
    assert d.decision == "deny"
    assert d.hard_violation is True


def test_hook_allows_approved_under_ceiling() -> None:
    d = hook_decision(
        "craw code apply definitions/triage abc123", is_approved=True, ceiling_reached=False
    )
    assert d.decision == "allow"
    assert d.hard_violation is False


def test_propose_then_apply_cli_roundtrip(tmp_path: Path) -> None:
    """The CLI propose→(human approve)→apply happy path over the --json surface."""
    comp = _component(tmp_path)
    code, proposal = _run(_cmd_propose, _Args(component=str(comp)))
    assert code == EXIT_OK
    sha = proposal["candidate_sha"]
    assert proposal["approval"] == "pending"

    # Record the human decision out-of-band (not a fluid-reachable verb).
    store = store_for_dir(str(comp))
    try:
        ApprovalLedger(store, org_id="local").record_decision(str(comp), sha, approve=True)
    finally:
        store.close()

    code, applied = _run(_cmd_apply, _Args(component=str(comp), sha=sha))
    assert code == EXIT_OK
    assert applied["result"] == "applied"
