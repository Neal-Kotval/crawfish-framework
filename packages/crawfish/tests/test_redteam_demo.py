"""CRA-189 — red-team demo driver: every escape attempt is DENIED + AUDITED.

This is the acceptance test for the security spine (CRA-178 broker + CRA-179 jail). It
runs the adversarial ``demo/redteam/attacks.py`` and asserts each of the six attack
classes FAILS — denied, and where applicable audited on the ledger via a
``JAIL_VIOLATION``/``SECRET_LEASE`` emission — and that the secret VALUE leaks nowhere.

Fully deterministic: the demo uses ``FakeJail`` + a fake broker/transport + canned
fixtures; no real sandbox spawn, no real network, no real secrets, no live model call.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

from crawfish.emission import EmissionKind, read_emissions

REPO_ROOT = Path(__file__).resolve().parents[3]
ATTACKS_PATH = REPO_ROOT / "demo" / "redteam" / "attacks.py"


def _load_attacks() -> ModuleType:
    """Import ``demo/redteam/attacks.py`` by path (the demo dir isn't on sys.path)."""
    spec = importlib.util.spec_from_file_location("redteam_attacks", ATTACKS_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Register before exec so dataclass field resolution can find the module.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def attacks() -> ModuleType:
    return _load_attacks()


@pytest.fixture(scope="module")
def report(attacks: ModuleType):  # noqa: ANN201 - module-local RedTeamReport type
    return attacks.run_red_team()


# -- the aggregate verdict --------------------------------------------------


def test_every_attack_is_denied(report) -> None:  # noqa: ANN001
    """The headline acceptance: all six attack classes fail."""
    failed = [o.name for o in report.outcomes if not o.denied]
    assert not failed, f"these attacks were NOT denied (spine regression): {failed}"
    assert report.all_denied
    assert len(report.outcomes) == 6


def test_demo_is_runnable(attacks: ModuleType) -> None:
    """The human-facing entrypoint runs cleanly (the dogfood `python attacks.py`)."""
    attacks.main()  # prints the verdict; must not raise


# -- 1. prompt injection ----------------------------------------------------


def test_prompt_injection_stays_fenced_data(attacks: ModuleType) -> None:
    """Injection text reaches the model only as fenced UNTRUSTED DATA, never instructions."""
    from crawfish.runtime.prompt import compile_prompt
    from crawfish.testing import INJECTION_INPUTS

    outcome = attacks.attack_prompt_injection()
    assert outcome.denied

    # Re-derive the prompt independently and assert the fence directly against the real
    # runtime/prompt.py compiler — the injection is below the UNTRUSTED DATA header only.
    definition = attacks._injection_definition()
    agent = definition.team.agents[0]
    injection = str(INJECTION_INPUTS["exfiltrate_secret"])
    prompt = compile_prompt(
        definition,
        agent,
        {"project": "acme/widgets", "ticket_body": injection},
    )
    instructions, sep, data = prompt.partition("--- UNTRUSTED DATA")
    assert sep, "fluid input must be fenced under an UNTRUSTED DATA header"
    assert injection not in instructions  # NOT in the instruction half
    assert injection in data  # present only as data
    assert "acme/widgets" in instructions  # static config IS trusted
    assert attacks.SECRET_VALUE not in prompt  # secret never reaches the model


# -- 2. folder escape -------------------------------------------------------


def test_folder_escape_denied_and_audited(attacks: ModuleType) -> None:
    store = attacks.make_store()
    outcome = attacks.attack_folder_escape(store)
    assert outcome.denied
    assert outcome.audited_kind == EmissionKind.JAIL_VIOLATION.value

    emissions = read_emissions(store, "redteam")
    violations = [e for e in emissions if e.kind is EmissionKind.JAIL_VIOLATION]
    assert violations, "a folder escape must leave a JAIL_VIOLATION on the ledger"
    v = violations[0]
    assert v.attrs["attempt"] == "/etc/shadow"
    assert v.attrs["kind"] == "folder_escape"
    assert v.tainted is True  # a denial is by definition untrusted-code activity


# -- 3. undeclared egress ---------------------------------------------------


def test_undeclared_egress_denied_and_audited(attacks: ModuleType) -> None:
    store = attacks.make_store()
    outcome = attacks.attack_undeclared_egress(store)
    assert outcome.denied
    assert outcome.audited_kind == EmissionKind.JAIL_VIOLATION.value

    emissions = read_emissions(store, "redteam")
    violations = [e for e in emissions if e.kind is EmissionKind.JAIL_VIOLATION]
    assert violations
    assert violations[0].attrs["kind"] == "undeclared_egress"
    assert attacks.ATTACKER_HOST in str(violations[0].attrs["attempt"])


# -- 4. secret exfiltration -------------------------------------------------


def test_secret_exfiltration_denied_value_never_leaks(attacks: ModuleType) -> None:
    store = attacks.make_store()
    outcome = attacks.attack_secret_exfiltration(store)
    assert outcome.denied, outcome.reason
    # The only successful lease was audited by REFERENCE, never value.
    assert outcome.audited_kind == EmissionKind.SECRET_LEASE.value

    emissions = read_emissions(store, "redteam")
    leases = [e for e in emissions if e.kind is EmissionKind.SECRET_LEASE]
    assert leases
    for e in leases:
        assert e.attrs["ref"] == attacks.SECRET_REF  # reference present
        assert attacks.SECRET_VALUE not in str(tuple(e.attrs.values()))  # value absent

    # The transport never received a credentialed call (no redirect ever succeeded).
    # And the secret value appears NOWHERE on the ledger for this run.
    for e in emissions:
        assert attacks.SECRET_VALUE not in str(tuple(e.attrs.values()))


def test_secret_value_appears_nowhere(attacks: ModuleType, report) -> None:  # noqa: ANN001
    """Belt-and-suspenders: scan the whole aggregate run for the secret value."""
    emissions = read_emissions(report.store, "redteam")
    blob = "".join(str(e.model_dump()) for e in emissions)
    assert attacks.SECRET_VALUE not in blob
    # Also assert it's not hiding in any attack outcome's reason/handle reprs.
    for o in report.outcomes:
        assert attacks.SECRET_VALUE not in o.reason
        assert attacks.SECRET_VALUE not in str(o.emissions)


# -- 5. taint laundering ----------------------------------------------------


def test_taint_laundering_denied(attacks: ModuleType) -> None:
    outcome = attacks.attack_taint_laundering()
    assert outcome.denied, outcome.reason
    # The underlying spine suite must itself pass (no laundering across any boundary).
    from crawfish.testing import assert_taint_conformance

    assert_taint_conformance()


# -- 6. static-only bypass --------------------------------------------------


def test_static_only_bypass_denied(attacks: ModuleType) -> None:
    outcome = attacks.attack_static_only_bypass()
    assert outcome.denied, outcome.reason
    # Each sub-vector is individually rejected by the real spine seams.
    from crawfish.core import Flow
    from crawfish.jail import FakeJail, JailPath, StaticOnlyError
    from crawfish.secrets import Grant, LeaseDenied, SecretRequest

    with pytest.raises(StaticOnlyError):
        FakeJail().run(["python"], allow_paths=[JailPath("/etc", flow=Flow.FLUID)])

    store = attacks.make_store()
    broker = attacks.make_broker(store)
    grant = Grant(package="n", secrets=(attacks.SECRET_REF,), egress=(attacks.ATTACKER_HOST,))
    with pytest.raises(LeaseDenied):
        broker.lease(
            SecretRequest(
                node_id="n",
                ref=attacks.SECRET_REF,
                destination=attacks.ATTACKER_HOST,
                ref_flow=Flow.FLUID,
            ),
            grant,
        )
