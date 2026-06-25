"""CRA-269 — --json schema-version negotiation between plugin and CLI.

The plugin and the CLI upgrade independently. These tests pin: every payload carries
``schema`` (major tag) + ``schema_version`` (major/minor); a minor bump is additive
(an old parser ignoring the new field still passes); a major mismatch surfaces a
``schema_skew`` ``craw.error.v1`` envelope + exit 4 (fail closed, never a crash); and
``craw code schema`` dumps the full map. No model call.
"""

from __future__ import annotations

import io
import json

import pytest

from crawfish.code import (
    EXIT_SECURITY,
    SCHEMA_VERSIONS,
    ErrorCode,
    SchemaSkew,
    emit_error,
    negotiate,
    schema_tag,
    schema_version,
)
from crawfish.code.cli import run_code


def test_every_command_has_a_major_minor_version() -> None:
    for cmd, ver in SCHEMA_VERSIONS.items():
        assert isinstance(ver, tuple) and len(ver) == 2
        assert schema_version(cmd) == {"major": ver[0], "minor": ver[1]}


def test_schema_tag_is_major_only() -> None:
    """The tag a parser keys off is major-only, so a minor bump keeps it stable."""
    assert schema_tag("code.describe") == "craw.code.describe.v1"
    assert schema_tag("error") == "craw.error.v1"


def test_minor_bump_is_additive_old_parser_still_negotiates() -> None:
    """A plugin understanding major 1 negotiates a (1, N) CLI regardless of the minor."""
    # Same major → negotiation passes; the returned minor is whatever the CLI emits.
    result = negotiate("code.describe", plugin_major=1)
    assert result["major"] == 1


def test_major_mismatch_raises_schema_skew() -> None:
    """A plugin built for a different major than the CLI emits raises SchemaSkew."""
    with pytest.raises(SchemaSkew) as exc:
        negotiate("code.describe", plugin_major=2)
    assert exc.value.command == "code.describe"
    assert exc.value.cli_major == 1
    assert exc.value.plugin_major == 2


def test_schema_skew_surfaces_a_non_retryable_envelope_exit_4() -> None:
    """A skew is surfaced as a schema_skew craw.error.v1 envelope, exit 4, fail closed."""
    try:
        negotiate("code.describe", plugin_major=2)
    except SchemaSkew as skew:
        buf = io.StringIO()
        rc = emit_error(
            ErrorCode.SCHEMA_SKEW,
            remediation="Upgrade the crawfish plugin to a build that understands major 2.",
            detail={
                "command": skew.command,
                "cli_major": skew.cli_major,
                "plugin_major": skew.plugin_major,
            },
            stream=buf,
        )
        assert rc == EXIT_SECURITY
        payload = json.loads(buf.getvalue())
        assert payload["code"] == "schema_skew"
        assert payload["retryable"] is False
        assert payload["detail"] == {
            "command": "code.describe",
            "cli_major": 1,
            "plugin_major": 2,
        }
    else:  # pragma: no cover - the skew must raise
        pytest.fail("negotiate did not raise SchemaSkew on a major mismatch")


def test_craw_code_schema_dumps_the_full_map(capsys: pytest.CaptureFixture[str]) -> None:
    """`craw code schema --json` dumps the {command: major.minor} map, snapshot-stable."""
    rc = run_code(["schema", "--json"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["schema"] == "craw.code.schema.v1"
    assert out["versions"] == {
        "code.adopt": "1.0",
        "code.apply": "1.0",
        "code.control": "1.0",
        "code.cost": "1.0",
        "code.dashboard": "1.0",
        "code.dashboard.optimize": "1.0",
        "code.dashboard.runs": "1.0",
        "code.deploy": "1.0",
        "code.describe": "1.0",
        "code.diagnose": "1.0",
        "code.estimate": "1.0",
        "code.explain": "1.0",
        "code.fleet": "1.0",
        "code.grant": "1.0",
        "code.init": "1.0",
        "code.lint": "1.0",
        "code.map": "1.0",
        "code.new": "1.0",
        "code.optimize": "1.0",
        "code.propose": "1.0",
        "code.provenance": "1.0",
        "code.reject": "1.0",
        "code.review": "1.0",
        "code.run": "1.0",
        "code.schema": "1.0",
        "code.sync": "1.0",
        "code.validate": "1.0",
        "error": "1.0",
    }
