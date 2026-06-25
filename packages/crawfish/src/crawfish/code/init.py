"""``craw code init [<dir>]`` — stand a ``craw code`` project up (CRA-245).

A bare ``craw init`` writes the authored tree but does nothing about the agent loop. RFC
§4.1: ``craw code init`` must do *more* — scaffold the canonical layout **and** open the
``.crawfish/`` ledger the dashboard reads, recording an init provenance row, and (when the
shipped plugin bundle is present) install it. It must **never** reach a live model or
resolve a secret.

Three composable steps behind one verb:

1. **Scaffold** — reuse :data:`crawfish.scaffold.FILES` to write the canonical folders +
   ``crawfish.toml`` + the secrets-by-reference ``.env.example``. Reconcile-friendly:
   **create only if absent**, recording skipped (existing) paths (the byte-for-byte
   re-entrancy guarantee is CRA-279, a later wave; this verb already never clobbers).
2. **Start the ledger** — create ``<dir>/.crawfish/`` and open the Store **through the
   protocol/factory only** (never importing a concrete backend), recording an init
   provenance row (``generated_by="craw-code-init"``, ``source_tainted=False``).
3. **Install the plugin** — copy the shipped ``crawfish/plugin/`` bundle into
   ``<dir>/.claude/plugins/crawfish/`` (disjoint from ``.claude/agents/`` that export
   owns) when it exists, and **pin** the bundle (UNFILED-PIN): record its ``bundle_sha256``
   + ``requires_crawfish`` range in ``crawfish.plugin.lock`` (:mod:`crawfish.code.plugin`)
   so a tampered or wrong-version bundle is detectable. Until the bundle ships this step is
   a clean no-op, so ``init`` is useful today and gains the install with zero change here.

A self-registering verb (``register(subparsers)``).
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from crawfish.code import (
    EXIT_OK,
    SCHEMA_VERSIONS,
    emit_json,
)

if TYPE_CHECKING:
    from crawfish.store.base import Store

VERB_NAME = "init"

# This verb's --json schema, seeded here (not by editing the shared registry).
SCHEMA_VERSIONS.setdefault("code.init", (1, 0))  # type: ignore[attr-defined]


def register(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    """Register ``craw code init`` on the ``code`` subparser group."""
    from crawfish.code.cli import add_common_args

    p = subparsers.add_parser(VERB_NAME, help="scaffold a craw code project + start the ledger")
    p.add_argument("dir", nargs="?", default=".", help="project directory (default: cwd)")
    p.add_argument("--name", default=None, help="project name (crawfish.toml [project].name)")
    p.add_argument(
        "--no-plugin", action="store_true", help="scaffold + ledger only, skip plugin install"
    )
    add_common_args(p)
    p.set_defaults(func=_cmd_init)


def _plugin_source() -> Path | None:
    """The shipped plugin bundle dir, or None if it is not present in this distribution.

    The bundle is an M3 deliverable; until it ships, plugin install is a clean no-op so
    ``init`` is useful today and gains the install step with no change here.
    """
    candidate = Path(__file__).resolve().parent.parent / "plugin"
    return candidate if candidate.is_dir() else None


def _scaffold(root: Path, *, name: str | None) -> tuple[list[str], list[str]]:
    """Write the canonical tree, create-only-if-absent. Returns (created, skipped_existing)."""
    from crawfish.scaffold import FILES

    created: list[str] = []
    skipped: list[str] = []
    for rel, content in FILES.items():
        path = root / rel
        if path.exists():
            skipped.append(rel)
            continue
        if name is not None and rel == "crawfish.toml":
            content = content.replace('name = "crawfish-app"', f'name = "{name}"', 1)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        created.append(rel)
    return sorted(created), sorted(skipped)


def _install_plugin(root: Path) -> dict[str, object] | None:
    """Copy the shipped plugin bundle into ``.claude/plugins/crawfish/`` (if present).

    Disjoint from ``.claude/agents/`` (export's namespace, RFC O-4); the whole ``.claude``
    tree is already excluded from the Definition content sha, so this never perturbs
    content identity. Returns the plugin descriptor for the --json payload, or None when no
    bundle ships in this distribution.
    """
    source = _plugin_source()
    if source is None:
        return None
    dest = root / ".claude" / "plugins" / "crawfish"
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(source, dest)
    # Pin the bundle (UNFILED-PIN): compute bundle_sha256 + requires_crawfish range and
    # record them in the framework's plugin-pin file so a tampered/wrong-version bundle is
    # detectable (`craw doctor`) and a skewed range fails closed (`craw code sync`).
    from crawfish.code.plugin import compute_pin, write_pin

    pin = compute_pin(dest)
    write_pin(pin, root)
    return {
        "installed": True,
        "name": pin.name,
        "version": pin.version,
        "path": ".claude/plugins/crawfish/",
        "bundle_sha256": pin.bundle_sha256,
        "requires_crawfish": pin.requires_crawfish,
    }


def _cmd_init(args: argparse.Namespace) -> int:
    """Scaffold + start the ledger (+ install the plugin when present). No model, no secret."""
    as_json: bool = getattr(args, "as_json", False)
    org: str = getattr(args, "org", "local")
    root = Path(args.dir)
    root.mkdir(parents=True, exist_ok=True)

    created, skipped = _scaffold(root, name=args.name)

    # Start the ledger: open the Store through the factory (never a concrete backend) and
    # record one init provenance row. A METRIC emission mirrors it onto the dashboard.
    store = _open_store(root)
    try:
        _record_init_provenance(store, root, org=org)
    finally:
        _close_store(store)

    plugin: dict[str, object] = {"installed": False}
    if not args.no_plugin:
        installed = _install_plugin(root)
        if installed is not None:
            plugin = installed

    payload: dict[str, object] = {
        "project": args.name or _project_name(root),
        "dir": str(root.resolve()),
        "scaffolded": created,
        "skipped_existing": skipped,
        "plugin": plugin,
        "ledger": {"started": True, "path": ".crawfish/"},
        "next_steps": [
            "craw code new definition my-agent",
            "craw dev definitions/triage-bot -i project=acme -i ticket_body=…",
        ],
    }
    if as_json:
        emit_json("code.init", payload, org=org)
    else:
        what = f"{len(created)} created" + (f", {len(skipped)} kept" if skipped else "")
        print(f"initialized craw code project at {root} ({what}); ledger started")
    return EXIT_OK


def _project_name(root: Path) -> str:
    """Read the project name from crawfish.toml (best-effort), else the dir name."""
    from crawfish.config import load_manifest

    try:
        return load_manifest(root).name
    except Exception:  # pragma: no cover - defensive; a malformed toml shouldn't crash init
        return root.resolve().name


def _record_init_provenance(store: Store, root: Path, *, org: str) -> None:
    """Record one init provenance row (``generated_by="craw-code-init"``)."""
    import hashlib

    from crawfish.provenance import record_file_provenance

    # Key the row by the project's crawfish.toml content (its identity anchor); the init
    # event is the audit marker "craw code init ran here", never a fluid value.
    toml = root / "crawfish.toml"
    content = toml.read_text() if toml.exists() else ""
    sha = hashlib.sha256(content.encode()).hexdigest()[:12]
    record_file_provenance(
        "crawfish.toml",
        sha,
        store=store,
        authored_by="craw-code-init",
        source_tainted=False,
        org_id=org,
    )


def _open_store(root: Path) -> Store:
    """Open the project's Store via the protocol/factory — never import a concrete backend."""
    from crawfish.manage import store_for_dir

    (root / ".crawfish").mkdir(parents=True, exist_ok=True)
    return store_for_dir(str(root))


def _close_store(store: Store) -> None:
    close = getattr(store, "close", None)
    if callable(close):
        close()
