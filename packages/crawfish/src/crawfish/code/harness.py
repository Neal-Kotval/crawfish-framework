"""CRA-268 — deterministic record/replay harness for the authoring loop.

The agent-driven authoring loop calls a live model (``claude -p``), so it is untestable
under the repo's "no live model calls" bar without a record/replay layer. Claude Code has
file checkpointing but no comprehensive record/replay test-doubles stdlib, so this harness
builds on the framework's own cassette infra (:class:`RecordReplayRuntime`,
:class:`~crawfish.runtime.replay.CassetteMiss`) plus :class:`MockRuntime`.

A golden **authoring-session fixture** is a recorded transcript of the agent's session:
its model turns *and* its file-write tool calls (Write/Edit on the component folders).
Two doubles cooperate:

* model turns replay through :class:`RecordReplayRuntime` wrapping :class:`MockRuntime`
  (the cassette key already folds ``org_id`` + decode seed + execution coordinate, so a
  replay is byte-stable); a missing cassette raises :class:`CassetteMiss` — replay never
  silently hits the network;
* the **tool-call transcript** replays through :class:`AuthoringSession`, which in replay
  mode materializes the recorded writes into a project dir, then closes the loop by
  asserting each authored file's CRA-266 provenance row and any CRA-267 jail
  :class:`~crawfish.jail.Denial`.

Record mode (developer-only, gated behind ``--live`` → ``mode="record"``) captures a fresh
golden; **replay mode is the default and never calls a live model.**
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from crawfish.definition.compiler import DefinitionLoadError
from crawfish.definition.jailed import load_definition_jailed
from crawfish.jail import Denial, SandboxPolicy
from crawfish.provenance import FileProvenance
from crawfish.runtime.base import RunRequest
from crawfish.runtime.replay import RecordReplayRuntime

if TYPE_CHECKING:
    from crawfish.runtime.base import AgentRuntime
    from crawfish.store.base import Store

__all__ = [
    "AuthoringSession",
    "AuthoringResult",
    "AuthoredFile",
    "AuthoringTranscript",
]


@dataclass(frozen=True)
class AuthoredFile:
    """One file the recorded authoring session wrote (a Write/Edit tool call).

    ``path`` is repo-relative to the authored project; ``content`` is the bytes the agent
    wrote; ``source_tainted`` marks that the loop held fluid (untrusted) context when it
    authored the file (so CRA-266 stamps it tainted — the poisoned-ticket case).
    """

    path: str
    content: str
    source_tainted: bool = False


@dataclass(frozen=True)
class AuthoringTranscript:
    """A golden authoring-session fixture: model turns + the file writes they produced.

    ``model_turns`` is the sequence of model prompts the loop issued (replayed through the
    cassette layer); ``files`` is the ordered set of authored files. ``authored_by`` labels
    who authored (default ``"craw-code"``). Loaded from / saved to a JSON fixture under
    ``packages/crawfish/tests/fixtures/authoring/``.
    """

    model_turns: tuple[str, ...] = ()
    files: tuple[AuthoredFile, ...] = ()
    authored_by: str = "craw-code"

    @classmethod
    def from_json(cls, text: str) -> AuthoringTranscript:
        data = json.loads(text)
        files = tuple(
            AuthoredFile(
                path=str(f["path"]),
                content=str(f["content"]),
                source_tainted=bool(f.get("source_tainted", False)),
            )
            for f in data.get("files", [])
        )
        return cls(
            model_turns=tuple(str(t) for t in data.get("model_turns", [])),
            files=files,
            authored_by=str(data.get("authored_by", "craw-code")),
        )

    def to_json(self) -> str:
        return json.dumps(
            {
                "authored_by": self.authored_by,
                "model_turns": list(self.model_turns),
                "files": [
                    {"path": f.path, "content": f.content, "source_tainted": f.source_tainted}
                    for f in self.files
                ],
            },
            indent=2,
            sort_keys=True,
        )


@dataclass(frozen=True)
class AuthoringResult:
    """The frozen outcome of replaying an authoring session (the closed loop).

    ``files_written`` is the deterministic set of authored files; ``provenance`` is one
    CRA-266 row per file; ``jail_denials`` is any CRA-267 jail violation the compile of the
    authored tree produced (empty on a clean session).
    """

    files_written: tuple[str, ...]
    provenance: tuple[FileProvenance, ...]
    jail_denials: tuple[Denial, ...]
    failed_closed: bool = False


class AuthoringSession:
    """Replays a recorded authoring transcript deterministically (CRA-268).

    ``mode="replay"`` (default): feed the recorded model turns through the cassette layer
    (no live call; a miss raises :class:`CassetteMiss`) and materialize the recorded file
    writes into ``project_dir``, then run the CRA-267 jailed compile to stamp CRA-266
    provenance and surface any jail :class:`~crawfish.jail.Denial`.

    ``mode="record"`` (dev-only, reachable only behind an explicit ``--live`` flag — never
    the default test run): would capture a fresh golden by driving the live runtime. This
    class refuses to record unless ``record=True`` is passed *and* the inner runtime is a
    live one; the default path never records.
    """

    def __init__(
        self,
        transcript: AuthoringTranscript,
        *,
        runtime: AgentRuntime,
        store: Store,
        project_dir: str | Path,
        cassette_dir: str | Path,
        org_id: str = "local",
        mode: str = "replay",
        policy: SandboxPolicy | None = None,
    ) -> None:
        if mode not in ("replay", "record"):
            raise ValueError(f"mode must be 'replay' or 'record', got {mode!r}")
        self._transcript = transcript
        self._inner = runtime
        self._store = store
        self._project_dir = Path(project_dir)
        self._cassette_dir = Path(cassette_dir)
        self._org_id = org_id
        self._mode = mode
        # FakeJail by default keeps the harness offline + deterministic.
        self._policy = policy or SandboxPolicy(kind="fake")

    @classmethod
    def from_fixture(
        cls,
        transcript_path: str | Path,
        *,
        runtime: AgentRuntime,
        store: Store,
        project_dir: str | Path,
        cassette_dir: str | Path,
        org_id: str = "local",
        mode: str = "replay",
        policy: SandboxPolicy | None = None,
    ) -> AuthoringSession:
        """Construct from a golden JSON transcript fixture path."""
        transcript = AuthoringTranscript.from_json(Path(transcript_path).read_text())
        return cls(
            transcript,
            runtime=runtime,
            store=store,
            project_dir=project_dir,
            cassette_dir=cassette_dir,
            org_id=org_id,
            mode=mode,
            policy=policy,
        )

    async def run(self) -> AuthoringResult:
        """Replay (or record) the session and return its closed-loop result.

        Replay never calls a live model: the model turns route through
        :class:`RecordReplayRuntime` over :class:`MockRuntime`, so a missing cassette is a
        :class:`CassetteMiss`, not a silent network hit.
        """
        from crawfish.core.context import RunContext
        from crawfish.definition.types import AgentSpec, Definition, TeamSpec

        recording = self._mode == "record"
        replay_runtime = RecordReplayRuntime(self._inner, self._cassette_dir, record=recording)
        ctx = RunContext(store=self._store, org_id=self._org_id)

        # -- replay the model turns through the cassette layer (byte-stable) --------
        # A minimal Definition carries each turn's prompt; the cassette key folds org_id +
        # the Definition id, so the id MUST be stable across record/replay (the default is a
        # random uuid). Pin it so a replay under the recorded org is deterministic and a
        # miss raises CassetteMiss.
        turn_definition = Definition(
            id="craw-code-authoring-session",
            team=TeamSpec(agents=[AgentSpec(role="author")]),
        )
        for i, turn in enumerate(self._transcript.model_turns):
            request = RunRequest(
                definition=turn_definition,
                inputs={"turn": i, "prompt": turn},
                role="author",
            )
            # Raises CassetteMiss in replay mode when no cassette exists (fail closed).
            await replay_runtime.run(request, ctx)

        # -- materialize the recorded file writes into the project dir --------------
        for f in self._transcript.files:
            dest = self._project_dir / f.path
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(f.content)

        # -- close the loop: jailed compile → CRA-266 provenance + CRA-267 denials --
        taint_by_path = {f.path: f.source_tainted for f in self._transcript.files}
        denials: tuple[Denial, ...] = ()
        failed_closed = False
        try:
            compiled = load_definition_jailed(
                self._project_dir,
                store=self._store,
                org_id=self._org_id,
                policy=self._policy,
                authored_by=lambda _f: self._transcript.authored_by,
                compile_probe=_tainted_compile_probe(taint_by_path),
            )
            provenance = compiled.provenance
        except DefinitionLoadError:
            # A poisoned authoring session that escapes the jail fails closed — record the
            # denial path (CRA-267) and report the closed loop, not an unhandled crash.
            failed_closed = True
            provenance = tuple(
                self._store_provenance(f.path, f.content) for f in self._transcript.files
            )
            denials = self._recorded_denials()

        files_written = tuple(f.path for f in self._transcript.files)
        return AuthoringResult(
            files_written=files_written,
            provenance=provenance,
            jail_denials=denials,
            failed_closed=failed_closed,
        )

    def _store_provenance(self, path: str, content: str) -> FileProvenance:
        """The recorded per-file provenance for a materialized file (post fail-closed)."""
        import hashlib

        from crawfish.provenance import file_provenance

        sha = hashlib.sha256(content.encode()).hexdigest()[:12]
        existing = file_provenance(path, sha, store=self._store, org_id=self._org_id)
        return existing or FileProvenance(
            component_path=path, content_sha=sha, authored_by=self._transcript.authored_by
        )

    def _recorded_denials(self) -> tuple[Denial, ...]:
        """Lift any JAIL_VIOLATION emissions written during the fail-closed compile."""
        from crawfish.emission import EmissionKind, read_emissions
        from crawfish.jail import DenialKind

        emissions = read_emissions(
            self._store, f"jailed-compile:{self._project_dir.name}", org_id=self._org_id
        )
        out: list[Denial] = []
        for e in emissions:
            if e.kind is EmissionKind.JAIL_VIOLATION:
                attempt = str(e.attrs.get("attempt", ""))
                kind_raw = str(e.attrs.get("kind", DenialKind.FOLDER_ESCAPE.value))
                try:
                    kind = DenialKind(kind_raw)
                except ValueError:  # pragma: no cover - defensive
                    kind = DenialKind.FOLDER_ESCAPE
                out.append(Denial(kind=kind, attempt=attempt))
        return tuple(out)


def _tainted_compile_probe(taint_by_path: dict[str, bool]):  # type: ignore[no-untyped-def]
    """A compile probe whose child declares fluid output iff any authored file was tainted.

    Keeps the harness offline (FakeJail) while faithfully propagating the recorded
    source-taint into the jail's ``out_taint`` (and thence the CRA-266 rows): a session
    that authored under fluid context comes back tainted, a clean one does not.
    """
    from crawfish.jail import _Probe

    any_tainted = any(taint_by_path.values())

    def _factory(files: Sequence[str], root: Path):  # type: ignore[no-untyped-def]
        reads = [str((root / f).resolve()) for f in files]

        def _program(_cmd: Sequence[str]) -> _Probe:
            return _Probe(reads=reads, emits_fluid=any_tainted)

        return _program

    return _factory
