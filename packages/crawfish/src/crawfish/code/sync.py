"""``craw code sync`` — reconcile the on-disk tree with discovery (CRA-247).

The self-generating loop (RFC §5) edits files and immediately calls into the project; the
agent needs a "where am I / is the tree healthy" call. ``sync`` composes three reads into
one reconciliation:

1. :mod:`crawfish.discovery` — enumerate the discovered components by kind.
2. :func:`crawfish.doctor.diagnose` — structure health (misplaced files, authored-vs-
   generated tamper) as structured findings.
3. per-Definition :func:`crawfish.definition.load_definition` — surface a
   :class:`~crawfish.definition.DefinitionLoadError` as a structured finding (exit 1), not
   a crash. Loading also **regenerates ``definition.lock``** for each Definition (the
   compiler writes it), so a freshly ``new``-ed or hand-edited component is reconciled.

**Critically**, for each Definition that loads it runs the **assembly gate**
(:func:`crawfish.build.assert_build_safe` → ALG-3 fluid→static-sink check, ``SECURITY.md``
rule) **before** declaring the tree runnable — so the edit→run loop can't skip it. A
fluid→static-sink wiring yields exit ``4`` (security rejection, non-retryable). ``sync``
reads only the filesystem + Store; it **never** runs a model or resolves a secret (mirrors
``craw doctor``'s safety contract).

A self-registering verb (``register(subparsers)``).
"""

from __future__ import annotations

import argparse
from pathlib import Path

from crawfish.code import (
    EXIT_EXPECTED_FAILURE,
    EXIT_OK,
    SCHEMA_VERSIONS,
    ErrorCode,
    emit_error,
    emit_json,
)

VERB_NAME = "sync"

# ``code.sync`` is pre-seeded by the Wave-1 foundation; setdefault keeps this idempotent
# (and forward-safe if the foundation ever drops it).
SCHEMA_VERSIONS.setdefault("code.sync", (1, 0))  # type: ignore[attr-defined]


def register(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    """Register ``craw code sync`` on the ``code`` subparser group."""
    from crawfish.code.cli import add_common_args

    p = subparsers.add_parser(VERB_NAME, help="reconcile the tree with discovery + doctor")
    p.add_argument("--dir", default=".", help="project directory (default: cwd)")
    add_common_args(p)
    p.set_defaults(func=_cmd_sync)


def _components(root: Path) -> dict[str, list[str]]:
    """Discovered components grouped by kind (the orientation read)."""
    from crawfish.discovery import LOCAL_DIRS, Registry

    reg = Registry.discover(root)
    out: dict[str, list[str]] = {}
    for kind, subdir in LOCAL_DIRS.items():
        # Key on the canonical folder name (definitions/pipelines/sources/sinks/policies/…)
        # so the component map matches the spec's keys, not a naive ``kind + "s"``.
        out[subdir] = sorted(r.name for r in reg.of_kind(kind))
    return out


def _drift_findings(root: Path) -> tuple[list[dict[str, object]], str]:
    """Doctor's structure findings projected to ``sync`` drift + the ledger verdict.

    Returns (drift_findings, ledger_verdict). ``ledger_verdict`` is ``"clean"`` unless an
    authored unit hides inside ``.crawfish/`` (doctor's ``error`` level), which is the
    authored-vs-generated tamper signal.
    """
    from crawfish.doctor import diagnose

    report = diagnose(root)
    drift: list[dict[str, object]] = []
    ledger = "clean"
    for f in report.findings:
        if f.level == "warn" and "looks like a Definition" in f.message:
            drift.append({"kind": "misplaced", "message": f.message})
        elif f.level == "error":
            # authored unit inside .crawfish/ — the tamper / generated-boundary breach
            ledger = "dirty"
            drift.append({"kind": "tamper", "message": f.message})
        elif f.level == "warn":
            drift.append({"kind": "warning", "message": f.message})
    return drift, ledger


def _definition_dirs(root: Path) -> list[Path]:
    """Every Definition-shaped directory under the (possibly relocated) definitions folder."""
    from crawfish.config import load_manifest

    defs_dir = root / load_manifest(root).paths.definitions
    if not defs_dir.is_dir():
        return []
    return sorted(
        child
        for child in defs_dir.iterdir()
        if child.is_dir()
        and ((child / "instructions.md").exists() or (child / "definition.py").exists())
    )


def _plugin_skew(root: Path) -> str | None:
    """The pinned plugin's compat verdict against the installed crawfish (UNFILED-PIN).

    Returns ``None`` when there is no plugin pin (nothing to check) or the pinned
    ``requires_crawfish`` range admits the installed version. Returns a static, human
    remediation string when the range **excludes** the installed version — the §12.3
    plugin-not-lockstepped gap, surfaced fail-closed so a stale plugin can't teach rules the
    framework no longer enforces.
    """
    from crawfish.code.plugin import (
        installed_crawfish_version,
        read_pin,
        requires_satisfied_by,
    )

    pin = read_pin(root)
    if pin is None:
        return None
    installed = installed_crawfish_version()
    if requires_satisfied_by(pin.requires_crawfish, installed):
        return None
    return (
        f"plugin bundle requires crawfish {pin.requires_crawfish!r} but {installed} is "
        f"installed; re-pin with `craw code init --upgrade`"
    )


def _cmd_sync(args: argparse.Namespace) -> int:
    """Reconcile the tree: components + drift + load-errors + the assembly-gate precondition."""
    as_json: bool = getattr(args, "as_json", False)
    org: str = getattr(args, "org", "local")
    root = Path(args.dir)

    # Plugin compat precondition (UNFILED-PIN): a pinned bundle whose requires_crawfish range
    # excludes the installed version fails closed before the tree is declared runnable.
    skew = _plugin_skew(root)
    if skew is not None:
        return emit_error(
            ErrorCode.PLUGIN_SKEW,
            retryable=True,  # recoverable: re-pin / align versions, then re-run
            remediation=skew,
            as_json=as_json,
        )

    components = _components(root)
    drift, ledger = _drift_findings(root)

    # Load each Definition (regenerates definition.lock) and run the assembly gate.
    from crawfish.build import assert_build_safe
    from crawfish.definition import DefinitionLoadError, load_definition

    load_errors: list[dict[str, object]] = []
    checked: list[str] = []
    rejected: list[str] = []
    for d in _definition_dirs(root):
        try:
            definition = load_definition(d)
        except DefinitionLoadError as exc:
            load_errors.append(
                {
                    "component": f"definitions/{d.name}",
                    "code": "DefinitionLoadError",
                    "message": str(exc),
                }
            )
            continue
        # Assembly gate precondition: a fluid->static-sink wiring fails closed here, so the
        # edit->run loop can never skip it (SECURITY.md rule; the §12.2 gap closed).
        try:
            assert_build_safe([definition])
            checked.append(d.name)
        except Exception as exc:  # FluidToStaticSinkError (and any ALG-3 rejection)
            rejected.append(d.name)
            return emit_error(
                ErrorCode.FLUID_TO_STATIC_SINK,
                remediation="a fluid (untrusted) value is wired toward a static-only sink "
                "target / idempotency key; consequential sink targets are static-only",
                detail={
                    "exit": 7,
                    "component": f"definitions/{d.name}",
                    # A static, type-shaped message only — never echo a fluid value back.
                    "rejection": type(exc).__name__,
                },
                as_json=as_json,
            )

    payload: dict[str, object] = {
        "components": components,
        "drift": drift,
        "load_errors": load_errors,
        "assembly_gate": {"checked": sorted(checked), "rejected": sorted(rejected)},
        "ledger": ledger,
    }
    if as_json:
        emit_json("code.sync", payload, org=org)
    else:
        _print_human(components, drift, load_errors, checked, rejected, ledger)

    # Exit 2 on .crawfish tamper; exit 1 on any drift/load-error; else 0 clean.
    if ledger == "dirty":
        return 2
    if drift or load_errors:
        return EXIT_EXPECTED_FAILURE
    return EXIT_OK


def _print_human(
    components: dict[str, list[str]],
    drift: list[dict[str, object]],
    load_errors: list[dict[str, object]],
    checked: list[str],
    rejected: list[str],
    ledger: str,
) -> None:
    """A terse human reconciliation summary (the non-``--json`` path)."""
    counts = ", ".join(f"{k}={len(v)}" for k, v in components.items() if v) or "none"
    print(f"components: {counts}")
    for f in drift:
        print(f"  drift [{f['kind']}]: {f['message']}")
    for e in load_errors:
        print(f"  load-error {e['component']}: {e['message']}")
    print(f"assembly gate: checked {sorted(checked)} rejected {sorted(rejected)}")
    print(f"ledger: {ledger}")
