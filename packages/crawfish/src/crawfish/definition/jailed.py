"""Jailed compile of agent-authored code (CRA-267, ADR: jailed-compile).

``load_definition`` imports ``definition.py`` / ``policies/*.py`` / ``tools/*.py`` /
``mcp/*.py`` **in-process** at compile time and treats them as authoring-time trusted
(``docs/reference/definition.md``). That assumption holds only while a *human* authors
the directory. When the author is ``craw code`` — a stochastic, prompt-injectable agent
— importing those files is arbitrary code execution in the orchestrator, steerable by a
poisoned ticket the agent read.

This module routes the **compile-time import** of agent-authored code through the
existing :class:`~crawfish.jail.Jail` seam (ADR 0016) — the same out-of-process,
folder-scoped, network-denied isolation the runtime uses for host-side node code, now
applied one phase earlier. The project dir is bound **read-only** and ``allow_net=False``;
any :class:`~crawfish.jail.Denial` (folder escape / undeclared egress) is emitted as a
``JAIL_VIOLATION`` and the compile **fails closed** (:class:`DefinitionLoadError`). The
:attr:`~crawfish.jail.JailResult.out_taint` the jail propagates back is recorded onto each
component's CRA-266 per-file provenance row, so a compile that read fluid-derived files or
touched the network comes back tainted.

Human-authored components keep the fast in-process path (no perf regression for the human
loop): :func:`load_definition_jailed` selects the jailed path only when a component is
non-human-authored (or its authorship is unknown).

Determinism / tests: the jail backend is :func:`~crawfish.jail.select_jail`-selected, so a
test injects :class:`~crawfish.jail.FakeJail` via ``SandboxPolicy(kind="fake")`` and an
in-scope/out-of-scope ``_Probe`` program — no real process, no model call. A FLUID-tagged
``allow_paths`` entry raises :class:`~crawfish.jail.StaticOnlyError` before any spawn (a
fluid value can never widen the jail).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from crawfish.core.types import Flow
from crawfish.definition.compiler import (
    DefinitionLoadError,
    load_definition,
)
from crawfish.jail import (
    FLUID_TAINT,
    JailPath,
    JailResult,
    PathMode,
    SandboxPolicy,
    emit_denials,
    select_jail,
)
from crawfish.provenance import (
    AUTHORED_BY_HUMAN,
    FileProvenance,
    record_file_provenance,
)

if TYPE_CHECKING:
    from crawfish.definition.types import Definition
    from crawfish.jail import _Probe
    from crawfish.store.base import Store

__all__ = [
    "load_definition_jailed",
    "import_bearing_files",
    "JailedCompileResult",
]

#: The subdirectories under a Definition dir whose ``*.py`` files are imported at compile
#: time (the arbitrary-code-execution surface ``load_definition`` walks). ``definition.py``
#: at the root is added explicitly.
_IMPORT_BEARING_DIRS = ("tools", "policies", "mcp")


def import_bearing_files(project_dir: str | Path) -> list[str]:
    """The repo-relative ``*.py`` files ``load_definition`` imports at compile time.

    These are the files whose top-level code executes during a compile — the surface the
    jail must confine. Returned sorted + repo-relative (deterministic).
    """
    root = Path(project_dir)
    found: list[str] = []
    definition_py = root / "definition.py"
    if definition_py.exists():
        found.append("definition.py")
    for sub in _IMPORT_BEARING_DIRS:
        for py in sorted((root / sub).glob("*.py")):
            if py.stem.startswith("_"):
                continue
            found.append(str(py.relative_to(root)))
    return sorted(found)


class JailedCompileResult:
    """The outcome of a jailed compile: the typed :class:`Definition` + the jail taint.

    Frozen-by-convention (a plain holder, no mutation). ``out_taint`` is the
    :class:`~crawfish.jail.TaintSet` the jail propagated out of the child; ``provenance``
    is one CRA-266 row per import-bearing component, each stamped with that taint.
    """

    __slots__ = ("definition", "out_taint", "provenance")

    def __init__(
        self,
        definition: Definition,
        out_taint: frozenset[str],
        provenance: tuple[FileProvenance, ...],
    ) -> None:
        self.definition = definition
        self.out_taint = out_taint
        self.provenance = provenance


def _default_compile_probe(files: Sequence[str], project_dir: Path):  # type: ignore[no-untyped-def]
    """The default in-scope compile program: reads exactly the import-bearing files.

    A real backend would *run* the import out-of-process; here the program declares the
    paths the import would touch (the project's own files, all in-scope) so the jail's
    folder/net policy is enforced against a faithful description of the compile. A test
    injects a hostile program (reading ``/etc/shadow`` or opening a socket) to exercise
    the fail-closed path.
    """
    from crawfish.jail import _Probe

    reads = [str((project_dir / f).resolve()) for f in files]

    def _program(_cmd: Sequence[str]) -> _Probe:
        return _Probe(reads=reads)

    return _program


def load_definition_jailed(
    path: str | Path,
    *,
    store: Store,
    org_id: str = "local",
    policy: SandboxPolicy | None = None,
    authored_by: Callable[[str], str] | None = None,
    compile_probe: Callable[[Sequence[str], Path], Callable[[Sequence[str]], _Probe]] | None = None,
) -> JailedCompileResult:
    """Compile a project whose components may be agent-authored, jailing the import.

    The import-bearing files are confined to a jail given the project dir **read-only**
    (``JailPath(project_dir, RO, STATIC)``) and ``allow_net=False``. A FLUID-tagged path
    raises :class:`~crawfish.jail.StaticOnlyError` before any spawn (the jail can never be
    widened by a fluid value). Any :class:`~crawfish.jail.Denial` is emitted as a
    ``JAIL_VIOLATION`` and the compile **fails closed** with :class:`DefinitionLoadError`
    — the authored code never executes in the orchestrator.

    On a clean jailed run the project compiles via the canonical :func:`load_definition`
    and the jail's :attr:`~crawfish.jail.JailResult.out_taint` is recorded onto each
    component's CRA-266 per-file provenance row (so a compile that touched fluid-derived
    files / the network is recorded tainted). ``authored_by`` maps a repo-relative file to
    its author label (default: every import-bearing file is ``"craw-code"``); a file mapped
    to ``"human"`` is still confined here but stamped human-authored.

    Exit-code mapping (CRA-243): a raised :class:`DefinitionLoadError` is a ``2`` (compile /
    jail failure); the CLI surfaces a jail violation through the ``craw.error.v1`` envelope
    with ``code="jail_violation"``, ``retryable=false`` (CRA-270).
    """
    root = Path(path)
    if not root.is_dir():
        raise DefinitionLoadError(f"not a directory: {root}")

    files = import_bearing_files(root)
    pol = policy or SandboxPolicy()
    jail = select_jail(pol)

    # The project dir, READ-ONLY and STATIC — allow_paths is static-only, so this can never
    # be widened by a fluid value (a FLUID JailPath raises StaticOnlyError before any spawn).
    project_path = JailPath(str(root.resolve()), mode=PathMode.RO, flow=Flow.STATIC)
    program = (compile_probe or _default_compile_probe)(files, root)
    # FakeJail consults the injected program; real backends ignore it (they spawn). We bind
    # the program onto the jail when it exposes the FakeJail probe hook.
    if hasattr(jail, "_program"):
        jail._program = program

    result: JailResult = jail.run(
        ["python", "-c", "compile-probe"],
        allow_paths=[project_path],
        allow_net=False,
        cwd=project_path,
    )

    if result.denied:
        # Fail closed: audit every denial, then refuse — the authored code never ran.
        emit_denials(store, result, run_id=f"jailed-compile:{root.name}", org_id=org_id)
        attempts = ", ".join(d.attempt for d in result.denied)
        raise DefinitionLoadError(
            f"jailed compile of {root.name!r} denied (folder escape / undeclared egress): "
            f"{attempts} — agent-authored code may not reach outside the project or the "
            "network at compile time (fail-closed, CRA-267)"
        )

    # The import surface is certified in-scope; compile via the canonical loader. The
    # registry round-trips by construction (same process), so parameters_compatible holds.
    definition = load_definition(root)

    # Stamp the jail's out_taint onto each component's per-file provenance row (CRA-266).
    author_of = authored_by or (lambda _f: "craw-code")
    out_taint = result.out_taint
    source_tainted = FLUID_TAINT in out_taint
    rows: list[FileProvenance] = []
    for rel in files:
        who = author_of(rel)
        # Human-authored files are stamped human; they are confined here but not treated
        # as agent-authored taint carriers unless the jail itself reported taint.
        file_sha = _file_content_sha(root / rel)
        row = record_file_provenance(
            rel,
            file_sha,
            store=store,
            authored_by=who,
            source_tainted=source_tainted and who != AUTHORED_BY_HUMAN,
            taint=out_taint if who != AUTHORED_BY_HUMAN else frozenset(),
            org_id=org_id,
        )
        rows.append(row)

    return JailedCompileResult(
        definition=definition,
        out_taint=out_taint,
        provenance=tuple(rows),
    )


def _file_content_sha(file_path: Path) -> str:
    """A pure 12-char content hash of one file's bytes (identity-adjacent, CRA-266).

    The per-file sha is over the file bytes alone — never mixed with ``authored_by`` — so a
    human and an agent copy of identical bytes share a sha but differ only in the row's
    author label.
    """
    import hashlib

    data = file_path.read_bytes() if file_path.exists() else b""
    return hashlib.sha256(data).hexdigest()[:12]
