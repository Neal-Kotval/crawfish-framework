"""``craw code adopt`` + ``craw code explain`` (UNFILED-ADOPT).

An existing Crawfish project (authored before ``craw code``, or via a bare ``craw init``)
needs to be brought into the agent loop **without re-scaffolding**: install the plugin +
ledger, export per-Definition Claude Code subagents, validate it loads clean, and print the
guided first-run. RFC O-4 (resolved, ADR 0012): ``adopt`` **subsumes
``craw export --claude-code``** as its export step, with **disjoint ``.claude/``
namespaces** — the plugin lives under ``.claude/plugins/crawfish/`` (reserved ``crawfish-*``
prefix), the per-Definition subagents under ``.claude/agents/`` (export's namespace). Both
are excluded from the Definition content sha, so adopt never perturbs content identity.

``adopt`` composes the verbs already built (reconcile via CRA-279, validate via ``map`` +
``sync``); it adds no new execution path. ``craw code explain <topic>`` is a thin **reader**
over the shipped docs (no model call, just file retrieval) — the human/agent orientation
surface for the security spine, the pipeline model, and the determinism discipline.

Two self-registering verbs (``register(subparsers)`` wires both).
"""

from __future__ import annotations

import argparse
from pathlib import Path

from crawfish.code import (
    EXIT_OK,
    SCHEMA_VERSIONS,
    ErrorCode,
    emit_error,
    emit_json,
)

VERB_NAME = "adopt"

# Both verbs' --json schemas, seeded here (not by editing the shared registry).
SCHEMA_VERSIONS.setdefault("code.adopt", (1, 0))  # type: ignore[attr-defined]
SCHEMA_VERSIONS.setdefault("code.explain", (1, 0))  # type: ignore[attr-defined]

#: ``explain`` topic → shipped doc (repo-relative). A thin reader: no model, just retrieval.
#: Resolved against the installed package's docs first, then the repo root (dev checkout).
_EXPLAIN_TOPICS: dict[str, str] = {
    "security-spine": "docs/architecture/SECURITY.md",
    "pipeline-model": "docs/guide/concepts.md",
    "determinism": "docs/guide/diff-prove-replay.md",
    "project-structure": "docs/guide/project-structure.md",
    "claude-code-export": "docs/guide/claude-code-export.md",
    "getting-started": "docs/guide/getting-started.md",
}


def register(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    """Register ``craw code adopt`` and ``craw code explain`` (both self-registering here)."""
    from crawfish.code.cli import add_common_args

    a = subparsers.add_parser(VERB_NAME, help="adopt an existing project into the agent loop")
    a.add_argument("dir", nargs="?", default=".", help="project directory (default: cwd)")
    a.add_argument("--no-export", action="store_true", help="skip the per-Definition CC export")
    add_common_args(a)
    a.set_defaults(func=_cmd_adopt)

    e = subparsers.add_parser("explain", help="print a shipped doc for a topic (no model call)")
    e.add_argument("topic", nargs="?", help=f"one of: {', '.join(sorted(_EXPLAIN_TOPICS))}")
    add_common_args(e)
    e.set_defaults(func=_cmd_explain)


def _docs_root() -> Path:
    """Locate the shipped ``docs/`` tree (repo root in a dev checkout)."""
    # packages/crawfish/src/crawfish/code/adopt.py -> repo root is parents[5].
    here = Path(__file__).resolve()
    for ancestor in here.parents:
        if (ancestor / "docs").is_dir() and (ancestor / "docs" / "guide").is_dir():
            return ancestor
    return here.parents[5]


def _cmd_adopt(args: argparse.Namespace) -> int:
    """Install plugin+ledger (reconcile), export CC subagents, validate via map+sync, guide."""
    as_json: bool = getattr(args, "as_json", False)
    org: str = getattr(args, "org", "local")
    root = Path(args.dir)

    # (1) Detect an existing project (crawfish.toml present) — else not_a_project. The
    # granular code 9 stays in detail.exit; the PROCESS exit is the CRA-243 usage family (2),
    # keeping the process-exit table closed at 0-4 (mirrors the approved 5/6 pattern).
    if not (root / "crawfish.toml").exists():
        return emit_error(
            ErrorCode.NOT_FOUND,
            remediation="not a Crawfish project (no crawfish.toml); run `craw code init` first",
            detail={"exit": 9, "reason": "not_a_project", "dir": str(root)},
            as_json=as_json,
        )

    # (2) Install the plugin + start the ledger ONLY IF absent (reconcile via CRA-279 init).
    from crawfish.code.init import _install_plugin, _open_store, _record_init_provenance

    had_ledger = (root / ".crawfish").is_dir()
    store = _open_store(root)
    try:
        if not had_ledger:
            _record_init_provenance(store, root, org=org)
    finally:
        store.close()
    plugin = _install_plugin(root) or {"installed": False}

    # (3) Export per-Definition CC subagents under .claude/agents/ (export's namespace —
    # disjoint from the plugin's .claude/plugins/crawfish/, ADR 0012). Carries no secrets.
    exported: list[dict[str, str]] = []
    if not args.no_export:
        exported = _export_definitions(root, org)

    # (4) Validate: map (count nodes/sinks) + sync (assembly gate + load errors), reusing the
    # already-built verbs so adoption runs the same gates as the steady-state loop.
    from crawfish.code.map import build_map

    map_body = build_map(root, org)
    nodes = map_body.get("nodes", [])
    node_list = nodes if isinstance(nodes, list) else []
    consequential = sum(1 for n in node_list if isinstance(n, dict) and n.get("consequential"))
    sync_rc = _validate_sync(root, org)

    payload: dict[str, object] = {
        "dir": str(root.resolve()),
        "plugin": plugin,
        "exported": exported,
        "map": {"nodes": len(node_list), "consequential_sinks": consequential},
        "validation": {"sync": "clean" if sync_rc == 0 else "issues", "sync_exit": sync_rc},
        "next_steps": [
            "craw dev definitions/<def> -i project=acme -i ticket_body=…",
            "craw code map --json",
        ],
    }
    if as_json:
        emit_json("code.adopt", payload, org=org)
    else:
        print(
            f"adopted {root}: plugin {'installed' if plugin.get('installed') else 'skipped'}, "
            f"{len(exported)} exported, sync {'clean' if sync_rc == 0 else 'issues'}"
        )
    return EXIT_OK


def _export_definitions(root: Path, org: str = "local") -> list[dict[str, str]]:
    """Run ``export_claude_code`` for each discovered Definition (export invariant: no secrets).

    ``adopt`` runs over an EXISTING, UNTRUSTED project BEFORE any consent gate, so the
    per-Definition compile MUST go through the jailed path (CRA-267): the project dir is
    bound RO+STATIC, ``allow_net=False``, and any jail Denial fails closed
    (:class:`DefinitionLoadError`) — a hostile ``tools/*.py`` with an import-time network
    connect / file read never executes in the orchestrator (it is surfaced by ``sync``,
    not exported). Mirrors :mod:`crawfish.code.describe` / ``estimate`` / ``harness``.
    """
    from crawfish.ccexport import export_claude_code
    from crawfish.definition import DefinitionLoadError
    from crawfish.definition.jailed import load_definition_jailed
    from crawfish.discovery import Registry
    from crawfish.jail import SandboxPolicy
    from crawfish.manage import store_for_dir

    out: list[dict[str, str]] = []
    reg = Registry.discover(root)
    # The jailed compile records CRA-266 provenance via the project Store (protocol-only).
    (root / ".crawfish").mkdir(parents=True, exist_ok=True)
    store = store_for_dir(str(root))
    try:
        for ref in reg.of_kind("definition"):
            try:
                compiled = load_definition_jailed(
                    ref.target, store=store, org_id=org, policy=SandboxPolicy(kind="fake")
                )
            except DefinitionLoadError:
                continue  # a broken / jailed-out Definition is surfaced by sync, not exported
            for path in export_claude_code(compiled.definition, root):
                out.append({"definition": ref.name, "file": str(path.relative_to(root))})
    finally:
        store.close()
    return out


def _validate_sync(root: Path, org: str) -> int:
    """Run ``craw code sync`` over the adopted tree and return its exit code."""
    from crawfish.code.cli import run_code

    return run_code(["sync", "--dir", str(root), "--org", org])


def _cmd_explain(args: argparse.Namespace) -> int:
    """Print the shipped doc body for a topic — a thin reader, no model call."""
    as_json: bool = getattr(args, "as_json", False)
    org: str = getattr(args, "org", "local")
    topic: str | None = getattr(args, "topic", None)

    if not topic or topic not in _EXPLAIN_TOPICS:
        return emit_error(
            ErrorCode.USAGE,
            remediation=f"unknown topic; choose one of: {', '.join(sorted(_EXPLAIN_TOPICS))}",
            detail={"topics": sorted(_EXPLAIN_TOPICS)},
            as_json=as_json,
        )

    doc_path = _docs_root() / _EXPLAIN_TOPICS[topic]
    if not doc_path.exists():
        return emit_error(
            ErrorCode.NOT_FOUND,
            remediation=f"the doc for {topic!r} is not present in this distribution",
            detail={"topic": topic, "path": _EXPLAIN_TOPICS[topic]},
            as_json=as_json,
        )

    body = doc_path.read_text()
    if as_json:
        emit_json(
            "code.explain", {"topic": topic, "path": _EXPLAIN_TOPICS[topic], "body": body}, org=org
        )
    else:
        print(body)
    return EXIT_OK
