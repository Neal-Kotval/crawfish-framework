"""Plugin bundle pin + integrity verification (UNFILED-PIN).

The shipped ``crawfish-*`` plugin is the **source of the security rules** an agent reads
(the security-spine skill et al.). An unpinned bundle can be silently swapped — a supply-
chain hole (SECURITY.md rule 6, §12.2). Claude Code has **no ``claude.lock``** (it resolves
plugin deps from the manifest's semver ranges), so the *framework* must pin the bundle by
content digest and a ``requires_crawfish`` compatibility range.

This module is the pure helper behind that pin:

* :func:`bundle_digest` — a deterministic ``sha256:<hex>`` over the plugin tree (sorted
  files, content-only, stable exclusions), mirroring the Definition content-sha discipline.
* :func:`read_manifest` — load ``.claude-plugin/plugin.json`` (the ``version`` +
  ``requires_crawfish`` range live here).
* :func:`write_pin` / :func:`read_pin` — persist/recover the :class:`PluginPin` in the
  framework's pin file (``crawfish.plugin.lock``; see the *spec correction* note below).
* :func:`verify_bundle` — re-verify an on-disk bundle against a recorded pin (``craw doctor``
  fail-closed tamper check).
* :func:`requires_satisfied_by` — the ``requires_crawfish`` range check (``craw code sync``
  fails closed on an incompatible range — the §12.3 plugin-not-lockstepped gap).

Pure: no network, no model, no concrete-backend import. ``(str, Enum)`` is not needed here
(no enums); dataclasses (not Pydantic) match the resolve-layer lock primitives this sits
beside (:class:`crawfish.resolve.Pin`).

Spec correction (UNFILED-PIN): the spec example writes the pin into ``crawfish.lock``. That
filename is already overloaded — :mod:`crawfish.build` treats ``crawfish.lock`` as a *pip
requirements* file (``pip install --requirement crawfish.lock``), and the resolve-closure
lock is the distinct ``crawfish.closure.lock``. To avoid corrupting the pip-requirements
file with a JSON document, the plugin pin gets its own unambiguous JSON home,
``crawfish.plugin.lock``. The pinned *fields* (``bundle_sha256`` + ``requires_crawfish``)
and the fail-closed semantics are exactly as specified.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

from crawfish.resolve import ResolutionError, SemVer

__all__ = [
    "PLUGIN_PIN_FILE",
    "PluginManifest",
    "PluginPin",
    "BundleMismatch",
    "bundle_digest",
    "read_manifest",
    "compute_pin",
    "write_pin",
    "read_pin",
    "verify_bundle",
    "requires_satisfied_by",
    "installed_crawfish_version",
]

#: The framework's plugin-pin file (a JSON document). Distinct from ``crawfish.lock`` (a pip
#: requirements file) and ``crawfish.closure.lock`` (the resolve closure) — see the module
#: docstring's spec-correction note.
PLUGIN_PIN_FILE = "crawfish.plugin.lock"

#: Path of the manifest inside a plugin bundle.
_MANIFEST_REL = Path(".claude-plugin") / "plugin.json"

#: Files/dirs excluded from the bundle digest (generated / non-content), mirroring the
#: Definition content-sha exclusion discipline so re-pinning is stable across machines.
_DIGEST_EXCLUDE_DIRS = frozenset({"__pycache__", ".git", ".crawfish"})
_DIGEST_EXCLUDE_SUFFIXES = frozenset({".pyc", ".pyo"})


class BundleMismatch(Exception):
    """An on-disk plugin bundle does not match its recorded pin. Fails closed.

    Raised by :func:`verify_bundle` when the recomputed :func:`bundle_digest` differs from
    the pinned ``bundle_sha256`` — the supply-chain tamper signal ``craw doctor`` surfaces.
    """


@dataclass(frozen=True)
class PluginManifest:
    """The fields of ``.claude-plugin/plugin.json`` the pin cares about."""

    name: str
    version: str
    requires_crawfish: str


@dataclass(frozen=True)
class PluginPin:
    """The recorded plugin pin — bundle digest + compat range (the integrity anchor).

    ``bundle_sha256`` is ``"sha256:<hex>"`` (the deterministic tree digest). ``requires_
    crawfish`` is the manifest's compat range (e.g. ``">=0.3,<0.4"``). Serialized as a JSON
    object under the top-level ``"plugin"`` key, exactly the spec's ``crawfish.lock``
    fragment shape (only the *file* differs — see the module docstring).
    """

    name: str
    version: str
    bundle_sha256: str
    requires_crawfish: str

    def to_dict(self) -> dict[str, object]:
        return {
            "plugin": {
                "name": self.name,
                "version": self.version,
                "bundle_sha256": self.bundle_sha256,
                "requires_crawfish": self.requires_crawfish,
            }
        }

    @classmethod
    def from_dict(cls, d: dict[str, object]) -> PluginPin:
        raw = d.get("plugin")
        if not isinstance(raw, dict):
            raise ResolutionError("plugin pin: missing top-level 'plugin' object")
        try:
            return cls(
                name=str(raw["name"]),
                version=str(raw["version"]),
                bundle_sha256=str(raw["bundle_sha256"]),
                requires_crawfish=str(raw["requires_crawfish"]),
            )
        except KeyError as exc:  # a hand-edited / truncated pin fails closed
            raise ResolutionError(f"plugin pin: missing field {exc.args[0]!r}") from exc


def _digest_files(bundle_dir: Path) -> list[Path]:
    """Every content file under ``bundle_dir`` that enters the digest, sorted (relative)."""
    files: list[Path] = []
    for path in bundle_dir.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(bundle_dir)
        if any(part in _DIGEST_EXCLUDE_DIRS for part in rel.parts):
            continue
        if path.suffix in _DIGEST_EXCLUDE_SUFFIXES:
            continue
        files.append(rel)
    return sorted(files)


def bundle_digest(bundle_dir: str | Path) -> str:
    """A deterministic ``"sha256:<hex>"`` over the plugin tree (sorted files, content-only).

    Hashes each included file's **relative path** (POSIX form) and **bytes**, in sorted path
    order — so the digest is stable across machines and independent of filesystem walk order.
    Excludes generated/non-content paths (``__pycache__``, ``*.pyc``, ``.git``, ``.crawfish``),
    mirroring the Definition content-sha exclusions.
    """
    root = Path(bundle_dir)
    if not root.is_dir():
        raise ResolutionError(f"plugin bundle not found: {root}")
    h = hashlib.sha256()
    for rel in _digest_files(root):
        # Path then a NUL separator then bytes then a record separator — an unambiguous
        # framing so two different trees can never collide by content/name reshuffling.
        h.update(rel.as_posix().encode("utf-8"))
        h.update(b"\x00")
        h.update((root / rel).read_bytes())
        h.update(b"\x1e")
    return "sha256:" + h.hexdigest()


def read_manifest(bundle_dir: str | Path) -> PluginManifest:
    """Load ``.claude-plugin/plugin.json`` from a bundle (data only — no code executes)."""
    manifest_path = Path(bundle_dir) / _MANIFEST_REL
    if not manifest_path.is_file():
        raise ResolutionError(f"plugin manifest not found: {manifest_path}")
    data = json.loads(manifest_path.read_text())
    if not isinstance(data, dict):
        raise ResolutionError("plugin manifest: top level is not an object")
    metadata = data.get("metadata") or {}
    requires = ""
    if isinstance(metadata, dict):
        requires = str(metadata.get("requires_crawfish", ""))
    return PluginManifest(
        name=str(data.get("name", "crawfish")),
        version=str(data.get("version", "0.0.0")),
        requires_crawfish=requires,
    )


def compute_pin(bundle_dir: str | Path) -> PluginPin:
    """Compute a :class:`PluginPin` for a bundle: its manifest fields + the tree digest."""
    manifest = read_manifest(bundle_dir)
    return PluginPin(
        name=manifest.name,
        version=manifest.version,
        bundle_sha256=bundle_digest(bundle_dir),
        requires_crawfish=manifest.requires_crawfish,
    )


def write_pin(pin: PluginPin, project_dir: str | Path) -> Path:
    """Write the pin to ``<project_dir>/crawfish.plugin.lock`` (deterministic JSON)."""
    path = Path(project_dir) / PLUGIN_PIN_FILE
    path.write_text(json.dumps(pin.to_dict(), indent=2, sort_keys=True) + "\n")
    return path


def read_pin(project_dir: str | Path) -> PluginPin | None:
    """Read the recorded pin, or ``None`` if the project has no plugin pin yet."""
    path = Path(project_dir) / PLUGIN_PIN_FILE
    if not path.is_file():
        return None
    return PluginPin.from_dict(json.loads(path.read_text()))


def verify_bundle(bundle_dir: str | Path, pin: PluginPin) -> None:
    """Re-verify an on-disk bundle against a recorded pin — **fail closed** on a mismatch.

    Recomputes :func:`bundle_digest` and compares it to ``pin.bundle_sha256``. A difference
    raises :class:`BundleMismatch` (the tamper signal ``craw doctor`` surfaces). This is the
    integrity check that keeps the security rules' source from being silently swapped.
    """
    actual = bundle_digest(bundle_dir)
    if actual != pin.bundle_sha256:
        raise BundleMismatch(
            f"plugin bundle digest mismatch: on-disk {actual} != pinned {pin.bundle_sha256}"
        )


# --------------------------------------------------------------------------- compat range
# A ``requires_crawfish`` range is a comma-joined conjunction of ``<op><version>`` clauses,
# e.g. ``">=0.3,<0.4"``. Each clause must hold for the installed version to be compatible.
_RANGE_CLAUSE_RE = re.compile(r"^\s*(>=|<=|>|<|==|=)?\s*(\d+(?:\.\d+){0,2})\s*$")


def _clause_satisfied(installed: SemVer, op: str, base: SemVer) -> bool:
    if op in ("", "=", "=="):
        return installed == base
    if op == ">=":
        return installed >= base
    if op == "<=":
        return installed <= base
    if op == ">":
        return installed > base
    if op == "<":
        return installed < base
    raise ResolutionError(f"plugin pin: unknown range operator {op!r}")


def requires_satisfied_by(requires_crawfish: str, installed_version: str) -> bool:
    """Does ``installed_version`` satisfy a ``requires_crawfish`` range (every clause holds)?

    An **empty** range is treated as "unconstrained" (compatible) — a bundle that declares
    no range is not *skewed*, just unpinned on that axis. A malformed clause fails closed
    (:class:`crawfish.resolve.ResolutionError`), never silently passes.
    """
    requires = requires_crawfish.strip()
    if not requires:
        return True
    installed = SemVer.parse(installed_version)
    for clause in requires.split(","):
        m = _RANGE_CLAUSE_RE.match(clause)
        if m is None:
            raise ResolutionError(f"plugin pin: invalid requires_crawfish clause {clause!r}")
        op, base = m.group(1) or "", SemVer.parse(m.group(2))
        if not _clause_satisfied(installed, op, base):
            return False
    return True


def installed_crawfish_version() -> str:
    """The installed ``crawfish`` version (``importlib.metadata``), falling back to the
    in-tree ``__version__`` when the distribution metadata is unavailable (editable dev)."""
    from importlib.metadata import PackageNotFoundError
    from importlib.metadata import version as _pkg_version

    try:
        return _pkg_version("crawfish")
    except PackageNotFoundError:  # pragma: no cover - editable/source checkout fallback
        from crawfish import __version__

        return __version__
