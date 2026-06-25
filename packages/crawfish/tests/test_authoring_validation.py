"""CRA-265 — the authoring validation eval.

The behavioural proof that the authoring playbook teaches the *enforced* shape: an agent who
follows it produces a Definition that loads jailed clean, passes the assembly gate, lints
clean, and runs green on the mock — and that the red-team negatives (a fluid→static-sink
wiring, an inline secret, an unknown tool binding) are **rejected by the real checks**, not
merely asserted in prose.

Deterministic: jailed compile via ``SandboxPolicy(kind="fake")``, mock run with a
record-shaped responder. No live model call, no network.
"""

from __future__ import annotations

from pathlib import Path

from crawfish.code.validate_authoring import (
    VALIDATE_SCHEMA,
    default_negatives,
    triage_responder,
    validate_authoring,
)
from crawfish.runtime.mock import MockRuntime
from crawfish.store import SqliteStore

_REPO = Path(__file__).resolve().parents[3]
_SPEC = _REPO / "docs" / "specs" / "craw-code" / "authoring" / "authoring-spec.toml"


def _run(tmp_path: Path) -> dict:
    store = SqliteStore()
    try:
        return validate_authoring(
            _SPEC,
            repo_root=_REPO,
            store=store,
            runtime=MockRuntime(responder=triage_responder()),
            tmp_root=tmp_path,
        )
    finally:
        store.close()


def test_eval_verdict_passes(tmp_path: Path) -> None:
    """The whole eval passes: golden clean + every negative rejected."""
    body = _run(tmp_path)
    assert body["schema"] == VALIDATE_SCHEMA
    assert body["verdict"] == "pass", body


def test_golden_positive_clears_every_stage(tmp_path: Path) -> None:
    """The golden loads jailed, passes the assembly gate + lint, and runs green on the mock."""
    body = _run(tmp_path)
    positives = body["positives"]
    assert len(positives) == 1
    row = positives[0]
    assert row["id"] == "craw-code-golden"
    assert row["loads"] is True
    assert row["assembly_gate"] == "pass"
    assert row["lint"] == "clean"
    assert row["test"] == "green"
    assert row["ok"] is True


def test_every_negative_is_rejected_by_the_real_gate(tmp_path: Path) -> None:
    """Each red-team fixture is rejected by its expected gate with the expected rejection."""
    body = _run(tmp_path)
    negatives = {n["id"]: n for n in body["negatives"]}

    # fluid→static-sink: the assembly gate (ALG-3) must raise FluidToStaticSinkError.
    fts = negatives["fluid-to-sink"]
    assert fts["rejected"] is True
    assert fts["rejected_by"] == "assembly_gate"
    assert fts["code"] == "FluidToStaticSinkError"

    # inline secret: the secret-shaped lint must flag it.
    sec = negatives["inline-secret"]
    assert sec["rejected"] is True
    assert sec["rejected_by"] == "secret_shaped_lint"

    # unknown tool binding: load must fail with DefinitionLoadError.
    unk = negatives["unknown-tool"]
    assert unk["rejected"] is True
    assert unk["rejected_by"] == "load"
    assert unk["code"] == "DefinitionLoadError"


def test_negatives_are_not_just_text_assertions(tmp_path: Path) -> None:
    """Sanity: the standard corpus is the three independent gates (no overlap, real rejections)."""
    cases = default_negatives()
    gates = {c.gate for c in cases}
    assert gates == {"assembly_gate", "secret_shaped_lint", "load"}
    # Each builder actually writes a directory that the gate then rejects (driven in the
    # eval above) — the gates are the real ALG-3 / lint / compiler, not a string match.
    assert {c.id for c in cases} == {"fluid-to-sink", "inline-secret", "unknown-tool"}
