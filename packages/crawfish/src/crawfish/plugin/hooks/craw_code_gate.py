#!/usr/bin/env python3
"""PreToolUse hook — the HARD backstop for the HITL gate (UNFILED-GATE).

This is the *enforcement* layer the RFC (§12.1) demands: a skill is a guideline an injected
agent can be talked out of; a PreToolUse hook is not. The Claude Code plugin ships this hook at
its root (``hooks/hooks.json``) so it intercepts **every Bash tool call** before it runs and can
hard-stop a consequential ``craw … --live`` / ``craw code apply`` promotion that lacks a recorded
human approval — even under ``--dangerously-skip-permissions`` / bypassPermissions mode.

It is intentionally thin: it parses the PreToolUse event off stdin, computes the two booleans the
**pure** decision function (:func:`crawfish.code.gate.hook_decision`) needs — ``is_approved`` and
``ceiling_reached`` — over the project's own approval ledger + cost gauge, and prints the decision
payload. All the policy lives in the importable, offline-testable ``hook_decision``; this wrapper
is the I/O shell.

Fail closed, by construction:

* A consequential command with **no** matching approved ``(component, sha)`` → ``deny`` + a hard
  violation → **exit 2** (hard-stops the tool call, overrides an ``allow`` rule / bypass mode).
* ``ceiling_reached`` → ``deny`` regardless of approval (the budget halt is load-bearing).
* ``is_approved`` is read from the gate's **own** ``code_approval`` record kind only — never from
  tainted ledger surface text, so a fluid-injected ``approved: true`` can never clear the gate.
* Any error resolving state is treated as *un*approved (deny a consequential command), never as a
  silent allow — an exception must never become an open door.

A non-consequential command is allowed (the hook is a backstop, not a blanket block).
"""

from __future__ import annotations

import json
import re
import sys

# Exit codes (mirrors the Claude Code hook contract): 0 = the printed JSON decision governs;
# 2 = a blocking error / hard stop that the runtime must honor even in bypass mode.
_EXIT_OK = 0
_EXIT_HARD_STOP = 2

# ``craw code apply <component> <sha>`` — the one consequential command that carries an explicit
# (component, sha) we can check against the approval ledger. Matched on the stable verb + args.
_APPLY_RE = re.compile(r"\bcraw\s+code\s+apply\s+(\S+)\s+(\S+)")


def _read_event() -> dict[str, object]:
    """Parse the PreToolUse event JSON from stdin (empty/garbage → an empty event)."""
    try:
        raw = sys.stdin.read()
        return json.loads(raw) if raw.strip() else {}
    except (json.JSONDecodeError, ValueError):
        return {}


def _command_of(event: dict[str, object]) -> str:
    """Extract the Bash command string from the PreToolUse event (best-effort, never raises)."""
    tool_input = event.get("tool_input")
    if isinstance(tool_input, dict):
        cmd = tool_input.get("command")
        if isinstance(cmd, str):
            return cmd
    return ""


def _project_of(event: dict[str, object]) -> str:
    """The project/cwd the command runs in (for the approval ledger + cost gauge)."""
    cwd = event.get("cwd")
    if isinstance(cwd, str) and cwd:
        return cwd
    return "."


def _resolve_state(command: str, project: str, org: str) -> tuple[bool, bool]:
    """Compute ``(is_approved, ceiling_reached)`` over the project's ledger + cost gauge.

    Fail closed: any error resolving the state returns ``(False, ...)`` so a consequential
    command with unresolvable approval state is denied, never silently allowed.
    """
    # ``is_approved`` only has meaning for an ``apply <component> <sha>``; a bare ``--live`` run
    # carries no sha, so it is treated as un-approved (the gate denies it).
    is_approved = False
    m = _APPLY_RE.search(command)
    if m is not None:
        component, sha = m.group(1), m.group(2)
        try:
            from crawfish.code.gate import ApprovalLedger
            from crawfish.manage import store_for_dir

            store = store_for_dir(project)
            try:
                is_approved = ApprovalLedger(store, org_id=org).is_approved(component, sha)
            finally:
                store.close()
        except Exception:  # noqa: BLE001 — unresolved approval == un-approved (fail closed)
            is_approved = False

    ceiling_reached = False
    try:
        from crawfish.code.dashboard import build_data

        data = build_data(project, org_id=org)
        try:
            ceiling_reached = data.cost_gauge().state == "ceiling_reached"
        finally:
            inner = getattr(data, "_store", None)
            close = getattr(inner, "close", None)
            if callable(close):
                close()
    except Exception:  # noqa: BLE001 — a missing ledger is "no ceiling", not a crash
        ceiling_reached = False

    return is_approved, ceiling_reached


def main() -> int:
    event = _read_event()
    command = _command_of(event)
    org = "local"
    project = _project_of(event)

    from crawfish.code.gate import hook_decision

    is_approved, ceiling_reached = _resolve_state(command, project, org)
    decision = hook_decision(command, is_approved=is_approved, ceiling_reached=ceiling_reached)
    print(json.dumps(decision.to_payload(), sort_keys=True))
    # A hard violation exits 2 — the hard stop that overrides an allow rule / bypass mode.
    return _EXIT_HARD_STOP if decision.hard_violation else _EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
