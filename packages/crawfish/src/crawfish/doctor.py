"""``craw doctor`` — project structure health.

Explains where things belong, flags misplaced/ambiguous files, and verifies the
split between **authored** code (the unit folders at the project root) and
**generated** state (``.crawfish/``). Reads the canonical layout, applying any
``crawfish.toml [project.paths]`` overrides so the report matches the real project.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from crawfish.config import ProjectPaths, load_manifest

__all__ = ["DoctorFinding", "DoctorReport", "diagnose", "CANONICAL_LAYOUT", "GENERATED_DIR"]

GENERATED_DIR = ".crawfish"

# The canonical project layout (folder -> what it holds). Authored at the root;
# generated state is isolated under ``.crawfish/``.
CANONICAL_LAYOUT: dict[str, str] = {
    "sources": "Source units — pull data in",
    "sinks": "Sink units — push results out",
    "definitions": "Definition packages — the agent teams",
    "pipelines": "Pipeline wiring — Source → Batch → Sink",
    "observers": "Observer units — watch running pipelines",
    "tools": "Custom tool functions",
    "policies": "Reusable policies",
}


class DoctorFinding(BaseModel):
    """One health observation. ``level`` is ``ok`` | ``info`` | ``warn`` | ``error``."""

    level: str
    message: str


class DoctorReport(BaseModel):
    findings: list[DoctorFinding] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True when nothing rose above ``info``."""
        return not any(f.level in {"warn", "error"} for f in self.findings)

    def add(self, level: str, message: str) -> None:
        self.findings.append(DoctorFinding(level=level, message=message))

    def text(self) -> str:
        glyph = {"ok": "✓", "info": "·", "warn": "!", "error": "✗"}
        lines = [f"  {glyph.get(f.level, '?')} {f.message}" for f in self.findings]
        verdict = "structure healthy" if self.ok else "structure needs attention"
        return "\n".join([*lines, f"\n{verdict}"])


def _looks_like_definition(d: Path) -> bool:
    return (d / "instructions.md").exists() or (d / "definition.py").exists()


def diagnose(project_dir: str | Path = ".") -> DoctorReport:
    """Inspect ``project_dir`` and return a structured structure-health report."""
    root = Path(project_dir)
    report = DoctorReport()

    manifest_path = root / "crawfish.toml"
    if manifest_path.exists():
        report.add("ok", "crawfish.toml present")
        paths = load_manifest(root).paths
    else:
        report.add("info", "no crawfish.toml — using the default layout")
        paths = ProjectPaths()

    path_map = paths.model_dump()
    defaults = ProjectPaths()
    for field, subdir in path_map.items():
        d = root / subdir
        default_sub = getattr(defaults, field)
        relocated = "" if subdir == default_sub else f" (relocated from {default_sub}/)"
        if d.is_dir():
            report.add("ok", f"{field}: {subdir}/{relocated}")
        elif relocated:
            # an explicit override that points nowhere is a real misconfiguration
            report.add("warn", f"{field}: configured {subdir}/ does not exist{relocated}")
        else:
            report.add("info", f"{field}: {subdir}/ not present (optional)")

    # Misplacement: a Definition-shaped directory sitting under a non-definition root.
    for field, subdir in path_map.items():
        if field in {"definitions", "observers"}:
            continue
        d = root / subdir
        if not d.is_dir():
            continue
        for child in sorted(d.iterdir()):
            if child.is_dir() and _looks_like_definition(child):
                report.add(
                    "warn",
                    f"{child} looks like a Definition but sits in {subdir}/ — "
                    f"move it to {paths.definitions}/",
                )

    # Generated-vs-authored separation.
    gen = root / GENERATED_DIR
    if gen.is_dir():
        report.add("ok", f"{GENERATED_DIR}/ holds generated state")
        gitignore = root / ".gitignore"
        ignored = gitignore.exists() and GENERATED_DIR in gitignore.read_text()
        if not ignored:
            report.add("warn", f"{GENERATED_DIR}/ should be gitignored (it is generated state)")
        # authored unit files must not hide inside generated state
        for sub in path_map.values():
            if (gen / sub).exists():
                report.add("error", f"authored {sub}/ found inside {GENERATED_DIR}/ — move it out")

    _check_plugin_pin(root, report)
    return report


def _check_plugin_pin(root: Path, report: DoctorReport) -> None:
    """Verify the installed plugin bundle against its recorded pin (UNFILED-PIN).

    Three outcomes: no pin → nothing to check (no finding); on-disk digest != pinned digest
    → ``error`` (the supply-chain tamper signal, fail closed); ``requires_crawfish`` range
    excludes the installed crawfish → ``warn`` (the plugin-not-lockstepped skew). The plugin
    bundle is the *source of the security rules*, so a silent swap must surface here.
    """
    from crawfish.code.plugin import (
        BundleMismatch,
        installed_crawfish_version,
        read_pin,
        requires_satisfied_by,
        verify_bundle,
    )

    pin = read_pin(root)
    if pin is None:
        return
    bundle_dir = root / ".claude" / "plugins" / "crawfish"
    if not bundle_dir.is_dir():
        report.add("warn", "plugin pinned in crawfish.plugin.lock but bundle not installed")
        return
    try:
        verify_bundle(bundle_dir, pin)
        report.add("ok", "plugin bundle matches its pinned digest")
    except BundleMismatch:
        report.add("error", "plugin bundle digest mismatch — bundle tampered or stale (re-pin)")
    except Exception as exc:  # a malformed/unreadable bundle is a real misconfiguration
        report.add("warn", f"plugin bundle could not be verified: {exc}")
        return
    installed = installed_crawfish_version()
    try:
        compatible = requires_satisfied_by(pin.requires_crawfish, installed)
    except Exception:  # a malformed range is itself a finding, never a crash
        report.add("warn", f"plugin requires_crawfish range is invalid: {pin.requires_crawfish!r}")
        return
    if not compatible:
        report.add(
            "warn",
            f"plugin requires crawfish {pin.requires_crawfish!r} but {installed} is installed",
        )
