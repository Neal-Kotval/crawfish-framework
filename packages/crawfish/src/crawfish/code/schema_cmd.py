"""``craw code schema`` — dump the emitted ``{command: "major.minor"}`` map (CRA-269).

The plugin does a one-shot compat check at session start (and after an ``--upgrade``) by
reading this map and comparing the majors it understands against what the CLI emits. This
is the introspection half of schema negotiation; the enforcement half (a ``schema_skew``
``craw.error.v1`` envelope on a major mismatch) lives in :func:`~crawfish.code.negotiate`
/ :func:`~crawfish.code.emit_error`.

A self-registering verb: it exposes ``register(subparsers)`` so
:func:`~crawfish.code.discover_verbs` wires it in with no edit to a shared dispatcher.
"""

from __future__ import annotations

import argparse

from crawfish.code import (
    SCHEMA_VERSIONS,
    emit_json,
)

VERB_NAME = "schema"


def register(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    """Register ``craw code schema`` on the ``code`` subparser group."""
    from crawfish.code.cli import add_common_args

    p = subparsers.add_parser(VERB_NAME, help="dump the --json schema-version map (CRA-269)")
    add_common_args(p)
    p.set_defaults(func=_cmd_schema)


def _cmd_schema(args: argparse.Namespace) -> int:
    """Emit the full ``{command: "major.minor"}`` map for a one-shot plugin compat check."""
    versions = {cmd: f"{major}.{minor}" for cmd, (major, minor) in sorted(SCHEMA_VERSIONS.items())}
    if getattr(args, "as_json", False):
        emit_json("code.schema", {"versions": versions}, org=getattr(args, "org", "local"))
    else:
        for cmd, mm in versions.items():
            print(f"{cmd:22} {mm}")
    return 0
