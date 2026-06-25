"""CRA-277 — re-enter the consent gate for agent-added MCP servers and secret refs.

When Claude (possibly steered by fluid data) authors a new ``MCPConnection`` or a new
``DefinitionRef`` dependency, it adds a **capability** — egress + a secret reference — that
would otherwise **bypass the install-time consent gate** (§12.2). Generated ≠ trusted: a
model-authored capability is never auto-trusted.

The framework already owns the enforcement seam:
:func:`crawfish.provenance.regate_generated` diffs a Definition's *newly* declared
(STATIC-only) capabilities against the prior :class:`~crawfish.secrets.Grant` and re-enters
``secrets.consent_install``, fail-closed via :class:`~crawfish.secrets.DenyConsent`. This
module wires that seam into the ``craw code`` surface:

* :func:`regate_definition` — the helper ``sync`` / ``adopt`` call after loading a
  Definition: if it newly declares an MCP/secret capability, re-gate it. Non-interactive
  (the agent loop) defaults to ``DenyConsent`` → :class:`~crawfish.provenance.ConsentRequired`
  → exit 4 (``consent_required`` is a security code, non-retryable). The consent surface
  shows secrets **by reference name only** (the prompt-injection spine — a fluid value can
  never name a secret), never a value.
* ``craw code grant <component>`` — the **human** approval entry point (a self-registering
  verb): an interactive ``--yes`` records the :class:`~crawfish.secrets.Grant` (the full
  declared surface) so the next unattended ``sync`` no longer re-prompts.

The re-gate carries ``source_tainted`` so the audit can flag a tainted-provenance artifact
(an MCP authored while the loop held a poisoned ticket in context).
"""

from __future__ import annotations

import argparse
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
    from crawfish.definition.types import Definition
    from crawfish.secrets import ConsentDecider
    from crawfish.store.base import Store

VERB_NAME = "grant"

# This verb's --json schema, seeded here (not by editing the shared registry).
SCHEMA_VERSIONS.setdefault("code.grant", (1, 0))  # type: ignore[attr-defined]


def regate_definition(
    definition: Definition,
    *,
    store: Store,
    org_id: str = "local",
    source_tainted: bool = False,
    decider: ConsentDecider | None = None,
) -> dict[str, object] | None:
    """Re-gate a Definition's newly-declared MCP/secret capabilities (CRA-277).

    Returns ``None`` when the Definition declares no capability the prior grant doesn't
    already cover (no re-consent needed). Raises
    :class:`~crawfish.provenance.ConsentRequired` when a new capability is declared and the
    decider declines (the fail-closed default in a non-interactive context). On consent,
    returns the granted-capability descriptor (references-only) for the JSON surface.
    """
    from crawfish.provenance import declared_capabilities, regate_generated
    from crawfish.secrets import GrantManifest

    declared = declared_capabilities(definition)
    if not declared.secrets and not declared.egress:
        return None  # nothing consequential declared — nothing to re-gate

    prior = GrantManifest(store, org_id=org_id).lookup(_package(definition))
    prior_secrets = set(prior.secrets) if prior is not None else set()
    prior_egress = set(prior.egress) if prior is not None else set()
    new_secrets = [s for s in declared.secrets if s not in prior_secrets]
    new_egress = [e for e in declared.egress if e not in prior_egress]
    if not new_secrets and not new_egress:
        return None  # already covered by the prior grant — no prompt

    # Re-enter the install-time consent gate (default DenyConsent → ConsentRequired).
    regate_generated(
        definition,
        store=store,
        generated_by="craw-code",
        decider=decider,
        source_tainted=source_tainted,
        org_id=org_id,
    )
    # Consent recorded — surface the newly granted capabilities by reference name only.
    return {
        "component": _package(definition),
        "new_capabilities": {"secrets": sorted(new_secrets), "egress": sorted(new_egress)},
        "decision": "granted",
    }


def _package(definition: Definition) -> str:
    """The grant key for a Definition (its id — the package consent is recorded against)."""
    return str(getattr(definition, "id", "")) or "definition"


def _project_root(component: Path) -> Path:
    """Walk up from a Definition dir to the project root (the dir holding ``crawfish.toml``).

    The grant lives in the project ledger so the next ``craw code sync`` (which reads the
    project Store) sees it. Falls back to the component's parent when no manifest is found.
    """
    for candidate in [component, *component.resolve().parents]:
        if (candidate / "crawfish.toml").exists():
            return candidate
    return component.resolve().parent


def register(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    """Register ``craw code grant`` — the human consent re-entry entry point."""
    from crawfish.code.cli import add_common_args

    p = subparsers.add_parser(
        VERB_NAME, help="consent to an agent-added MCP/secret capability (CRA-277)"
    )
    p.add_argument("component", help="path to the Definition directory (e.g. definitions/triage)")
    p.add_argument(
        "--yes",
        action="store_true",
        help="approve the declared capabilities (references-only; never a value)",
    )
    add_common_args(p)
    p.set_defaults(func=_cmd_grant)


def _cmd_grant(args: argparse.Namespace) -> int:
    """Re-gate a Definition interactively: ``--yes`` records the grant, else fail closed (4)."""
    as_json: bool = getattr(args, "as_json", False)
    org: str = getattr(args, "org", "local")
    component = Path(args.component)

    if not component.is_dir():
        return emit_error(
            ErrorCode.NOT_FOUND,
            remediation=f"component {args.component!r} not found; pass a Definition directory",
            detail={"component": args.component},
            as_json=as_json,
        )

    from crawfish.definition import DefinitionLoadError
    from crawfish.definition.jailed import load_definition_jailed
    from crawfish.jail import SandboxPolicy
    from crawfish.manage import store_for_dir
    from crawfish.provenance import ConsentRequired
    from crawfish.secrets import AutoConsent

    # --yes is the explicit human approval (AutoConsent); without it, default-deny.
    decider: ConsentDecider | None = AutoConsent() if args.yes else None
    # The grant is recorded in the PROJECT ledger (the dir holding crawfish.toml), the same
    # Store `craw code sync` reads — not the Definition subdir — so the next sync sees it.
    project_root = _project_root(component)
    (project_root / ".crawfish").mkdir(parents=True, exist_ok=True)
    store = store_for_dir(str(project_root))
    try:
        # The component is agent-authorable and re-gating it imports its code at compile time,
        # so the compile goes through the **jailed** path (CRA-267): project dir RO+STATIC,
        # ``allow_net=False``, a jail Denial fails closed (DefinitionLoadError) — a hostile
        # ``tools/*.py`` never executes unjailed in the orchestrator. Mirrors describe/estimate.
        try:
            definition = load_definition_jailed(
                component, store=store, org_id=org, policy=SandboxPolicy(kind="fake")
            ).definition
        except DefinitionLoadError as exc:
            return emit_error(
                ErrorCode.COMPILE_ERROR,
                remediation="the component failed to compile; fix it before granting",
                detail={"component": args.component, "message": str(exc)},
                as_json=as_json,
            )
        try:
            granted = regate_definition(definition, store=store, org_id=org, decider=decider)
        except ConsentRequired:
            return emit_error(
                ErrorCode.CONSENT_REQUIRED,
                remediation="this component declares a new MCP/secret capability; "
                "re-run with --yes to consent (generated capabilities are not auto-trusted)",
                detail={"component": args.component},
                as_json=as_json,
            )
    finally:
        store.close()

    payload: dict[str, object] = {
        "component": args.component,
        "decision": "granted" if granted else "no_new_capabilities",
        "new_capabilities": (granted or {}).get("new_capabilities", {"secrets": [], "egress": []}),
    }
    if as_json:
        emit_json("code.grant", payload, org=org)
    else:
        print(f"grant: {payload['decision']} for {args.component}")
    return EXIT_OK
