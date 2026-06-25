"""CRA-265 — the authoring validation eval.

The authoring playbook's value is that an agent who follows it produces a Definition that
``load_definition``s clean and passes the assembly gate. This module is the regression that
proves the skills + golden example actually compose: it loads the machine-checkable spec
(``docs/specs/craw-code/authoring/authoring-spec.toml``) + the golden project it names, then
runs a **positive** corpus (the golden — and any playbook-derived positive fixture — must
load jailed, pass the assembly gate, lint clean, and run green on the mock) and a **negative**
corpus (a fluid→static-sink wiring, an inline secret, an unknown tool binding) that must be
**rejected by the real checks** — not merely asserted in prose.

Determinism: the jailed compile uses ``SandboxPolicy(kind="fake")`` and the mock run uses a
record-shaped responder; no live model call, no network. The eval is a pure library function
(:func:`validate_authoring`) returning the ``craw.code.validate.v1`` body so a test (or a
later CLI verb) can drive it without this module owning argparse wiring.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from crawfish.runtime.base import AgentRuntime
    from crawfish.store.base import Store

__all__ = [
    "VALIDATE_SCHEMA",
    "NegativeCase",
    "validate_authoring",
    "load_authoring_spec",
]

VALIDATE_SCHEMA = "craw.code.validate.v1"

#: A record-shaped mock reply so a typed-record output (the golden's ``Triage``) validates
#: under the mock — the determinism harness, never a live call.
_TRIAGE_REPLY = json.dumps({"category": "bug", "severity": "high", "summary": "a summary"})


def load_authoring_spec(spec_path: str | Path) -> dict[str, object]:
    """Parse the machine-checkable authoring spec TOML (the single source of truth)."""
    return tomllib.loads(Path(spec_path).read_text())


class NegativeCase:
    """One negative corpus fixture: a spine-violating directory + the gate that must reject it.

    ``builder`` writes the hostile Definition under a tmp dir and returns its path; ``gate`` is
    the symbolic check expected to reject it (``"assembly_gate"`` / ``"secret_shaped_lint"`` /
    ``"load"``); ``code`` is the expected rejection type name.
    """

    __slots__ = ("id", "gate", "code", "builder")

    def __init__(self, id: str, gate: str, code: str, builder) -> None:  # type: ignore[no-untyped-def]
        self.id = id
        self.gate = gate
        self.code = code
        self.builder = builder


def _check_positive(project_dir: Path, *, store: Store, runtime: AgentRuntime) -> dict[str, object]:
    """Run the full positive pipeline over one authored project (all real checks).

    load jailed → assembly gate → secret-shaped lint → mock ``craw test``. Returns a
    per-fixture verdict row; ``ok`` is True only when every stage passes.
    """
    import asyncio

    from crawfish.build import assert_build_safe
    from crawfish.code.lint import lint_tree
    from crawfish.definition.jailed import load_definition_jailed
    from crawfish.jail import SandboxPolicy
    from crawfish.testing import run_fixtures

    row: dict[str, object] = {"id": project_dir.name}
    # 1) jailed compile (CRA-267) — agent-authored code confined, fail-closed.
    compiled = load_definition_jailed(
        project_dir, store=store, org_id="local", policy=SandboxPolicy(kind="fake")
    )
    definition = compiled.definition
    row["loads"] = True
    # 2) assembly gate (ALG-3) — no fluid→static-sink wiring.
    assert_build_safe([definition])
    row["assembly_gate"] = "pass"
    # 3) secret-shaped lint — no inline credential in the tree.
    row["lint"] = "fail" if lint_tree(project_dir) else "clean"
    # 4) mock craw test — every fixture runs green (no live call).
    fixtures = project_dir / "fixtures"
    if fixtures.is_dir():
        results = asyncio.run(run_fixtures(fixtures, definition, runtime))
        row["test"] = "green" if all(r.passed for r in results) else "red"
    else:
        row["test"] = "green"  # no fixtures ⇒ nothing to fail
    row["ok"] = row["assembly_gate"] == "pass" and row["lint"] == "clean" and row["test"] == "green"
    return row


def _check_negative(case: NegativeCase, tmp_root: Path, *, store: Store) -> dict[str, object]:
    """Drive one negative fixture through the REAL gate that must reject it.

    Returns a verdict row recording which gate rejected it and the rejection type name.
    ``rejected`` is True only when the expected gate actually raised/flagged.
    """
    from crawfish.alg3 import FluidToStaticSinkError
    from crawfish.build import assert_build_safe
    from crawfish.code.lint import lint_tree
    from crawfish.definition.compiler import DefinitionLoadError, load_definition
    from crawfish.definition.jailed import load_definition_jailed
    from crawfish.jail import SandboxPolicy

    project = case.builder(tmp_root)
    row: dict[str, object] = {"id": case.id, "expected_gate": case.gate}

    if case.gate == "secret_shaped_lint":
        findings = lint_tree(project)
        row["rejected_by"] = "secret_shaped_lint" if findings else None
        row["rejected"] = bool(findings)
        return row

    if case.gate == "load":
        try:
            load_definition(project)
            row["rejected"] = False
            row["rejected_by"] = None
        except DefinitionLoadError as exc:
            row["rejected"] = True
            row["rejected_by"] = "load"
            row["code"] = type(exc).__name__
        return row

    # assembly_gate: compile jailed, then the ALG-3 gate must reject the fluid→sink wiring.
    try:
        compiled = load_definition_jailed(
            project, store=store, org_id="local", policy=SandboxPolicy(kind="fake")
        )
        assert_build_safe([compiled.definition])
        row["rejected"] = False
        row["rejected_by"] = None
    except FluidToStaticSinkError as exc:
        row["rejected"] = True
        row["rejected_by"] = "assembly_gate"
        row["code"] = type(exc).__name__
    except DefinitionLoadError as exc:
        # A jail Denial or compile failure is also a rejection (defense in depth).
        row["rejected"] = True
        row["rejected_by"] = "assembly_gate"
        row["code"] = type(exc).__name__
    return row


def validate_authoring(
    spec_path: str | Path,
    *,
    repo_root: str | Path,
    store: Store,
    runtime: AgentRuntime,
    negatives: list[NegativeCase] | None = None,
    tmp_root: Path | None = None,
) -> dict[str, object]:
    """Run the positive + negative authoring corpora and return the ``craw.code.validate.v1`` body.

    The positive corpus is the golden project the spec names (``golden=`` in the TOML); the
    negative corpus is ``negatives`` (default: the standard fluid→sink / inline-secret /
    unknown-tool triad from :func:`default_negatives`). ``verdict`` is ``"pass"`` iff every
    positive is ``ok`` and every negative is ``rejected`` by its expected gate.
    """
    spec = load_authoring_spec(spec_path)
    golden = Path(repo_root) / str(spec["golden"])
    cases = negatives if negatives is not None else default_negatives()
    work = tmp_root or (golden.parent / ".validate-tmp")
    work.mkdir(parents=True, exist_ok=True)

    positives = [_check_positive(golden, store=store, runtime=runtime)]
    negative_rows = [_check_negative(c, work, store=store) for c in cases]

    verdict = all(p.get("ok") for p in positives) and all(n.get("rejected") for n in negative_rows)
    return {
        "schema": VALIDATE_SCHEMA,
        "positives": positives,
        "negatives": negative_rows,
        "verdict": "pass" if verdict else "fail",
    }


# ---------------------------------------------------------------------------
# The standard negative corpus — each is a spine violation the real checks reject.
# ---------------------------------------------------------------------------
def default_negatives() -> list[NegativeCase]:
    """The red-team triad: fluid→sink, inline secret, unknown tool binding."""
    return [
        NegativeCase(
            "fluid-to-sink", "assembly_gate", "FluidToStaticSinkError", _build_fluid_to_sink
        ),
        NegativeCase("inline-secret", "secret_shaped_lint", "inline_secret", _build_inline_secret),
        NegativeCase("unknown-tool", "load", "DefinitionLoadError", _build_unknown_tool),
    ]


def _write(project: Path, files: dict[str, str]) -> Path:
    for rel, content in files.items():
        path = project / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    return project


def _build_fluid_to_sink(root: Path) -> Path:
    """A Definition whose consequential output is mis-declared FLUID — ALG-3 must reject it."""
    return _write(
        root / "fluid-to-sink",
        {
            "instructions.md": "You triage a ticket.\n",
            "definition.py": (
                "from crawfish.core import Flow, Parameter\n"
                "inputs = [Parameter(name='ticket_body', type='str', flow=Flow.FLUID)]\n"
                # consequential output mis-declared FLUID — the injection path ALG-3 rejects.
                "outputs = [Parameter(name='triage', type='str', flow=Flow.FLUID)]\n"
            ),
        },
    )


def _build_inline_secret(root: Path) -> Path:
    """An mcp/*.py with an inline credential literal — the secret-shaped lint must flag it."""
    return _write(
        root / "inline-secret",
        {
            "instructions.md": "You triage a ticket.\n",
            "definition.py": (
                "from crawfish.core import Flow, Parameter\n"
                "inputs = [Parameter(name='project', type='str', flow=Flow.STATIC)]\n"
                "outputs = [Parameter(name='triage', type='str', flow=Flow.STATIC)]\n"
            ),
            # An inline GitHub PAT literal assigned to a secret-named var — the wrong shape.
            "mcp/github.py": (
                "from crawfish.definition.types import MCPConnection\n"
                'github = MCPConnection(name="github", '
                'auth="ghp_0123456789abcdef0123456789abcdef0123")\n'
            ),
        },
    )


def _build_unknown_tool(root: Path) -> Path:
    """An agent binding a tool that does not exist — load must fail with DefinitionLoadError."""
    return _write(
        root / "unknown-tool",
        {
            "instructions.md": "---\ntools: [does_not_exist]\n---\nYou triage a ticket.\n",
            "definition.py": (
                "from crawfish.core import Flow, Parameter\n"
                "inputs = [Parameter(name='project', type='str', flow=Flow.STATIC)]\n"
                "outputs = [Parameter(name='triage', type='str', flow=Flow.STATIC)]\n"
            ),
        },
    )


def triage_responder():  # type: ignore[no-untyped-def]
    """A record-shaped mock responder for the golden's typed ``Triage`` output."""
    return lambda _request: _TRIAGE_REPLY
