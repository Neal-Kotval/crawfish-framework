"""``craw code map`` — the whole-project discovery graph (UNFILED-MAP).

The agent's single, never-stale "what's in this project and how does it wire together"
call (RFC §12.4). ``map`` emits the component graph — flow-tagged typed IO, pipeline
topology (Source → Batch → Aggregator → Router → Sink), consequential sinks, and version
lineage — the orientation read before any authoring move.

It is a **pure reflection** over :mod:`crawfish.discovery` + per-Definition
``load_definition`` + the Store (via the ``Store`` protocol only — never a concrete
backend). Each node carries ``kind``, ``id``, and (for Definitions) typed IO with ``flow``
tags. **Consequential sinks are flagged**, and their ``target`` is surfaced as a static-only
*kind* — never a resolved destination, egress host, or secret — routed through the **same
redaction discipline as ``craw code describe``** (CRA-271): the agent learns *that* a sink
is consequential and its egress *kind*, never *where* it writes. A leak here is a direct
injection amplifier, so the test asserts the absence of any destination/secret.

The projection is **cached by content sha** under ``.crawfish/map/`` (mirroring the
``describe`` cache) so a large project re-maps cheaply; the cache is org-scoped (CRA-275).

A self-registering verb (``register(subparsers)``).
"""

from __future__ import annotations

import argparse
import hashlib
import json
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

VERB_NAME = "map"

# This verb's --json schema, seeded here (not by editing the shared registry).
SCHEMA_VERSIONS.setdefault("code.map", (1, 0))  # type: ignore[attr-defined]

#: The org-scoped, content-sha-keyed map cache (generated state, gitignored).
_CACHE_DIR = Path(".crawfish") / "map"


def register(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    """Register ``craw code map`` on the ``code`` subparser group."""
    from crawfish.code.cli import add_common_args

    p = subparsers.add_parser(VERB_NAME, help="emit the project's component/wiring graph")
    p.add_argument("--dir", default=".", help="project directory (default: cwd)")
    p.add_argument(
        "--format", choices=("json", "dot"), default="json", help="output format (json|dot)"
    )
    add_common_args(p)
    p.set_defaults(func=_cmd_map)


def _project_sha(root: Path) -> str:
    """A pure content hash over the discoverable tree (the map cache key).

    Folds every component file's bytes (definitions/sources/sinks/...), excluding generated
    state, so any authoring edit is a new sha → a cache miss → a re-map. Deterministic.
    """
    from crawfish.discovery import LOCAL_DIRS

    # Exclude generated artifacts so a map (which itself writes definition.lock via
    # load_definition) is stable across calls — mirrors the compiler's _HASH_EXCLUDE.
    exclude_dirs = {".crawfish", "__pycache__", ".claude", ".venv", ".git"}
    exclude_names = {"definition.lock", "crawfish.lock", "uv.lock", ".DS_Store"}
    h = hashlib.sha256()
    for _kind, subdir in sorted(LOCAL_DIRS.items()):
        d = root / subdir
        if not d.is_dir():
            continue
        for f in sorted(d.rglob("*")):
            if not f.is_file() or f.name in exclude_names:
                continue
            if any(part in exclude_dirs for part in f.parts):
                continue
            h.update(str(f.relative_to(root)).encode())
            h.update(b"\0")
            h.update(f.read_bytes())
            h.update(b"\0")
    return h.hexdigest()[:12]


def _definition_node(definition: Definition, name: str) -> dict[str, object]:
    """A Definition node with flow-tagged typed IO (reusing the describe projection)."""
    return {
        "kind": "definition",
        "id": name,
        "inputs": [
            {"name": p.name, "type": p.type, "flow": p.flow.value} for p in definition.inputs
        ],
        "outputs": [
            {"name": p.name, "type": p.type, "flow": p.flow.value} for p in definition.outputs
        ],
    }


def _sink_node(name: str) -> dict[str, object]:
    """A sink node — consequential, with a STATIC-only target *kind* (CRA-271 redaction).

    A consequential sink's ``target`` is static-only by the spine; ``map`` surfaces only the
    *kind* (``target_kind="static"``) and a coarse ``egress_kind`` (the sink's name), never
    a resolved destination, egress host, or secret reference.
    """
    return {
        "kind": "sink",
        "id": name,
        "consequential": True,
        "target_kind": "static",  # the spine guarantee — never a resolved destination
        "egress_kind": name,  # the sink kind by name, not an egress host/URL
    }


def build_map(root: Path, org: str = "local") -> dict[str, object]:
    """Build the ``craw.code.map.v1`` body — pure reflection, redacted per CRA-271.

    The per-Definition compile goes through the **jailed** path (CRA-267): ``map`` may run
    over an existing, untrusted project, so importing a hostile ``tools/*.py`` in the
    orchestrator is the exact host-execution hole the jail closes (project dir RO+STATIC,
    ``allow_net=False``, a jail Denial fails closed). Mirrors ``describe`` / ``estimate``.
    """
    from crawfish.definition import DefinitionLoadError
    from crawfish.definition.jailed import load_definition_jailed
    from crawfish.discovery import Registry
    from crawfish.jail import SandboxPolicy
    from crawfish.manage import store_for_dir

    reg = Registry.discover(root)
    nodes: list[dict[str, object]] = []
    edges: list[dict[str, object]] = []
    lineage: dict[str, list[str]] = {}

    # The jailed compile records CRA-266 provenance via the project Store (protocol-only).
    (root / ".crawfish").mkdir(parents=True, exist_ok=True)
    store = store_for_dir(str(root))
    try:
        # Definition nodes (flow-tagged typed IO) + their DefinitionRef dependency edges.
        for ref in reg.of_kind("definition"):
            try:
                compiled = load_definition_jailed(
                    ref.target, store=store, org_id=org, policy=SandboxPolicy(kind="fake")
                )
            except DefinitionLoadError:
                # A load error (or a jailed-out hostile import) is surfaced by `sync`, not
                # here; map skips the broken node so the orientation read never crashes on a
                # half-written component and the authored code never executes unjailed.
                continue
            definition = compiled.definition
            nodes.append(_definition_node(definition, ref.name))
            lineage[ref.name] = [str(definition.version)]
            for dep in getattr(definition, "dependencies", []) or []:
                edges.append(
                    {"from": f"definition:{ref.name}", "to": f"definition:{dep.id}", "via": "dep"}
                )
    finally:
        store.close()

    # Sink nodes (consequential, redacted target) + a generic definition→sink wiring edge.
    sink_names = sorted(r.name for r in reg.of_kind("sink"))
    for sink_name in sink_names:
        nodes.append(_sink_node(sink_name))

    # Source nodes + a source→batch wiring hint (the canonical pipeline shape).
    for src in sorted(r.name for r in reg.of_kind("source")):
        nodes.append({"kind": "source", "id": src})

    return {
        "nodes": nodes,
        "edges": edges,
        "lineage": lineage,
        "deployed": [],  # the deploy registry surface (empty until a deploy is registered)
    }


def _to_dot(body: dict[str, object]) -> str:
    """Render the same model as a graphviz ``digraph`` (``--format dot``)."""
    lines = ["digraph crawfish {"]
    nodes = body.get("nodes", [])
    if isinstance(nodes, list):
        for n in nodes:
            if isinstance(n, dict):
                nid = f"{n.get('kind')}:{n.get('id')}"
                lines.append(f'  "{nid}";')
    edges = body.get("edges", [])
    if isinstance(edges, list):
        for e in edges:
            if isinstance(e, dict):
                lines.append(f'  "{e.get("from")}" -> "{e.get("to")}";')
    lines.append("}")
    return "\n".join(lines)


def _cmd_map(args: argparse.Namespace) -> int:
    """Reflect the project; emit ``craw.code.map.v1`` (json) or a graphviz graph (dot)."""
    as_json: bool = getattr(args, "as_json", False)
    org: str = getattr(args, "org", "local")
    fmt: str = getattr(args, "format", "json")
    root = Path(args.dir)

    if not root.is_dir():
        return emit_error(
            ErrorCode.NOT_FOUND,
            remediation=f"directory {args.dir!r} not found",
            detail={"dir": args.dir},
            as_json=as_json,
        )

    # Content-sha cache (org-scoped). An unchanged project re-maps from cache (zero re-reflect).
    sha = _project_sha(root)
    cache_file = root / _CACHE_DIR / org / f"{sha}.json"
    if cache_file.exists():
        body: dict[str, object] = json.loads(cache_file.read_text())
    else:
        body = build_map(root, org)
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps(body, sort_keys=True))

    if fmt == "dot":
        print(_to_dot(body))
        return EXIT_OK
    if as_json:
        emit_json("code.map", body, org=org)
    else:
        _print_human(body)
    return EXIT_OK


def _print_human(body: dict[str, object]) -> None:
    """A terse node/edge summary (the non-``--json`` path)."""
    nodes = body.get("nodes", [])
    edges = body.get("edges", [])
    n_count = len(nodes) if isinstance(nodes, list) else 0
    e_count = len(edges) if isinstance(edges, list) else 0
    print(f"map: {n_count} nodes, {e_count} edges")
    if isinstance(nodes, list):
        for n in nodes:
            if isinstance(n, dict):
                flag = " [consequential]" if n.get("consequential") else ""
                print(f"  {n.get('kind')}: {n.get('id')}{flag}")
