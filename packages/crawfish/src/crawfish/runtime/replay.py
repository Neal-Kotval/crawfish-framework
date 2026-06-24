"""Record/replay — deterministic runs from cassettes.

Wrap any runtime. On a cache hit, replay the recorded ``RunResult`` (zero cost, no
model call — `craw dev` and `craw test` iterate without burning budget). On a miss in
``record`` mode, call the inner runtime and persist the cassette; otherwise raise.

Cassette identity (``_key``) is the **execution coordinate** of a run. Beyond the
Definition's content hash (id + version + role) and the bound run inputs, it folds:

* an optional, content-hashed :class:`ExecutionCoordinate` — *which* re-run of a leaf
  this is (quorum ``sample_index``, ``Refine`` ``iter_index``, MCTS ``visit_count``,
  ``recurse`` ``depth``), so operators that re-run the same leaf get **distinct**
  cassettes instead of colliding into one;
* the tenant ``org_id`` (from :class:`RunContext`), so cross-tenant cassettes never
  collide;
* any decode-control field on the request that is **not** already inside the Definition
  content hash (today: ``decode_seed``), read defensively via ``getattr`` so a
  concurrently-added field is picked up without a hard dependency on it.

**Back-compat guarantee:** every extra component is folded in *only when it is
non-default*. With no coordinate, ``org_id == "local"``, and no decode field, ``_key``
reproduces the exact legacy key — so legacy unsalted cassettes still resolve.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from pathlib import Path

from crawfish.core.context import RunContext
from crawfish.runtime.base import AgentRuntime, RunRequest, RunResult
from crawfish.runtime.prompt import pick_agent

__all__ = ["RecordReplayRuntime", "CassetteMiss", "ExecutionCoordinate"]


class CassetteMiss(RuntimeError):
    """Raised when no cassette exists and recording is disabled."""


@dataclasses.dataclass(frozen=True)
class ExecutionCoordinate:
    """Which re-run of a leaf this is — the run's position in an operator's fan-out.

    Every field is optional and defaults to ``None``; an all-``None`` coordinate is
    treated as *absent* and folds nothing into the cassette key (legacy back-compat).
    Operators that re-run the same leaf stamp the relevant axis:

    * ``sample_index`` — quorum/best-of-k sample (0..k-1);
    * ``iter_index``   — ``Refine`` iteration;
    * ``visit_count``  — MCTS / search visit;
    * ``depth``        — ``recurse`` recursion depth.
    """

    sample_index: int | None = None
    iter_index: int | None = None
    visit_count: int | None = None
    depth: int | None = None

    def is_empty(self) -> bool:
        """True when no axis is set — i.e. the coordinate contributes nothing."""
        return all(v is None for v in dataclasses.astuple(self))

    def as_canonical(self) -> dict[str, int]:
        """The non-default axes, as a deterministic dict for content hashing."""
        return {
            name: value for name, value in dataclasses.asdict(self).items() if value is not None
        }


def _key(
    request: RunRequest,
    *,
    org_id: str = "local",
    coordinate: ExecutionCoordinate | None = None,
) -> str:
    """Canonical cassette key for a run.

    Component order (the canonical dict, ``sort_keys`` JSON):
    ``id, version, role, model, inputs, session_id`` (legacy core), then — appended
    **only when non-default** — ``coordinate`` (content-hashed axes), ``org_id``
    (when not ``"local"``), and ``decode_seed`` (when present on the request).
    """
    agent = pick_agent(request.definition, request.role)
    canonical: dict[str, object] = {
        "id": request.definition.id,
        "version": str(request.definition.version),
        "role": agent.role,
        "model": request.model,
        "inputs": request.inputs,
        "session_id": request.session_id,
    }

    # Fold extra components ONLY when non-default, so the legacy key is byte-for-byte
    # reproduced (no coordinate, org_id == "local", no decode field).
    if coordinate is not None and not coordinate.is_empty():
        canonical["coordinate"] = coordinate.as_canonical()
    if org_id != "local":
        canonical["org_id"] = org_id
    # Decode-control field that is NOT in the Definition content hash. Read defensively:
    # another agent (F-5) may add this field to RunRequest concurrently.
    decode_seed = getattr(request, "decode_seed", None)
    if decode_seed is not None:
        canonical["decode_seed"] = decode_seed

    blob = json.dumps(canonical, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


class RecordReplayRuntime(AgentRuntime):
    name = "replay"

    def __init__(
        self, inner: AgentRuntime, cassette_dir: str | Path, *, record: bool = False
    ) -> None:
        self._inner = inner
        self._dir = Path(cassette_dir)
        self._record = record

    async def run(
        self,
        request: RunRequest,
        ctx: RunContext,
        *,
        coordinate: ExecutionCoordinate | None = None,
    ) -> RunResult:
        key = _key(request, org_id=ctx.org_id, coordinate=coordinate)
        path = self._dir / f"{key}.json"
        if path.exists():
            result = RunResult.model_validate_json(path.read_text())
            self._emit_telemetry(ctx, result, f"replay:{self._inner.name}")
            return result  # zero cost — no budget charge on replay
        if not self._record:
            raise CassetteMiss(f"no cassette for request (key {path.stem}); run with record=True")
        result = await self._inner.run(request, ctx)
        self._dir.mkdir(parents=True, exist_ok=True)
        path.write_text(result.model_dump_json(indent=2))
        return result
