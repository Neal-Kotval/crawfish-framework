"""CRA-243 — audit the --json coverage + exit-code contract for agent use.

``craw code`` drives the project by parsing ``craw … --json`` over Bash, so every verb
must emit a versioned ``craw.<cmd>.v<N>`` payload through one shared emitter and return a
meaningful exit code from the uniform table. These tests pin the shared contract (the
``code/`` subpackage substrate every later verb builds on): the exit-code table, the
``emit_json`` envelope, the self-registering verb registry, and the coverage matrix doc.
No model call.
"""

from __future__ import annotations

import json

import pytest

from crawfish.code import (
    EXIT_BUDGET,
    EXIT_CODES,
    EXIT_EXPECTED_FAILURE,
    EXIT_OK,
    EXIT_SECURITY,
    EXIT_USAGE,
    discover_verbs,
    emit_json,
    schema_tag,
    schema_version,
)
from crawfish.code.cli import run_code


def test_exit_code_table_is_the_documented_uniform_table() -> None:
    """The exit-code table is the CRA-243 contract: 0/1/2/3/4 with stable meanings."""
    assert (EXIT_OK, EXIT_EXPECTED_FAILURE, EXIT_USAGE, EXIT_BUDGET, EXIT_SECURITY) == (
        0,
        1,
        2,
        3,
        4,
    )
    assert EXIT_CODES == {
        "ok": 0,
        "expected_failure": 1,
        "usage": 2,
        "budget_exceeded": 3,
        "security_rejection": 4,
    }


def test_emit_json_wraps_payload_in_a_versioned_sorted_envelope(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The single emitter adds schema/schema_version/org and sorts keys (CRA-269 negotiated)."""
    emit_json("code.schema", {"b": 2, "a": 1}, org="acme")
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["schema"] == schema_tag("code.schema")
    assert payload["schema_version"] == schema_version("code.schema")
    assert payload["org"] == "acme"
    assert payload["a"] == 1 and payload["b"] == 2
    # sort_keys=True → the serialized text is canonical (snapshot-stable).
    assert out == json.dumps(payload, sort_keys=True) + "\n"


def test_registry_auto_discovers_self_registering_verbs() -> None:
    """Verbs are discovered via pkgutil — adding a file adds a verb (no shared dispatcher)."""
    hooks = discover_verbs()
    assert hooks, "no craw code verbs discovered"
    # The schema verb (CRA-269) is one of them, registered by file, not by an edit.
    rc = run_code(["schema", "--json"])
    assert rc == EXIT_OK


def test_schema_verb_emits_a_versioned_payload(capsys: pytest.CaptureFixture[str]) -> None:
    """A craw code verb under --json emits a craw.<cmd>.v<N> payload (the agent surface)."""
    rc = run_code(["schema", "--json"])
    assert rc == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"].startswith("craw.code.schema.v")
    assert "schema_version" in payload


def test_bare_craw_code_returns_zero_and_lists_verbs(capsys: pytest.CaptureFixture[str]) -> None:
    """`craw code` with no verb is usage (exit 0), not an error envelope."""
    rc = run_code([])
    assert rc == EXIT_OK
    out = capsys.readouterr().out
    assert "schema" in out  # the registered verb is listed


def test_coverage_matrix_doc_is_present_and_consistent() -> None:
    """The verb × has-json × exit-codes coverage matrix is checked into the docs.

    The matrix lists the uniform exit-code table; this asserts it stays in sync with the
    code (a regression that changes a code without updating the doc fails CI).
    """
    from pathlib import Path

    repo = Path(__file__).resolve().parents[3]
    matrix = repo / "docs" / "specs" / "craw-code" / "json-exit-code-matrix.md"
    assert matrix.exists(), f"coverage matrix missing at {matrix}"
    text = matrix.read_text()
    for name, value in EXIT_CODES.items():
        assert f"`{value}`" in text, f"exit code {value} ({name}) not documented in the matrix"
    assert "craw.error.v1" in text  # the error envelope row is documented
