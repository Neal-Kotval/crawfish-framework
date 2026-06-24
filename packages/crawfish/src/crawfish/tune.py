"""The tunable knob space as data — Axis 1 of the two-axis mode unifier (CRA-209 / AL-T1).

These are the *light* tune types: a :class:`KnobDomain`, a :class:`TuneSpec` (the typed form of
``tune.toml``), and :func:`tune_spec_sha`. They are split out from :mod:`crawfish.tuner` because
``Definition.tune`` annotates :class:`TuneSpec`, and ``crawfish.definition.types`` must resolve
that type at schema-build time — but :mod:`crawfish.tuner` sits behind a heavy import cycle
(``tuner → eval → metrics → batch → definition.types``). This module has **no crawfish imports**
(only ``pydantic``/``json``/``hashlib``/``tomllib``), so ``definition.types`` can import it without
a cycle, and :mod:`crawfish.tuner` re-exports the same class objects (so ``from crawfish.tuner
import TuneSpec`` keeps working and stays identity-stable for round-trips).

PyTorch's hardest lesson: ``requires_grad`` and ``.eval()`` are *orthogonal* axes. Axis 1
(tunable) is **DATA** — which knobs may move is a :class:`TuneSpec`, content-hashed into the
Definition (authored as ``tune.toml``), not imperative code at a call site. The security boundary
is upheld because a knob *domain* is author config, not session data: it carries no free model
text and never reads a fluid value.
"""

from __future__ import annotations

import hashlib
import json
import tomllib
from collections.abc import Iterator

from pydantic import BaseModel, Field

__all__ = ["KnobValue", "KnobDomain", "TuneSpec", "tune_spec_sha"]


# A scalar JSON leaf a knob may take. Kept narrow on purpose: a knob domain is static,
# author-supplied config (it enters the content hash), never a fluid/model-derived value.
KnobValue = str | int | float | bool | None


class KnobDomain(BaseModel):
    """One tunable knob: where it lives (``path``), its candidate ``values``, and whether
    the Tuner is *allowed* to move it (``tunable``).

    ``path`` is a dotted address into the Definition's knob space — the authoring vocabulary
    the mutators already speak: ``agent.<role>.prompt`` / ``.model`` / ``.temperature`` /
    ``.sample_k`` / ``.context_strategy`` / ``.policies``, ``team.coordination``,
    ``injected_prompts``. ``tunable=False`` pins the knob: it is declared (so its domain is
    documented and hashed) but :meth:`TuneSpec.named_knobs` will not yield it and a
    TuneSpec-driven mutator must refuse to move it.
    """

    model_config = {"frozen": True}

    path: str
    values: list[KnobValue] = Field(default_factory=list)
    tunable: bool = True


class TuneSpec(BaseModel):
    """Axis 1 as data: the set of knobs a Tuner may search, content-hashable + authorable.

    This is the typed form of ``tune.toml``. It is *static config* — it enters the
    Definition's content identity via :func:`tune_spec_sha` (folded into ``Definition.tune``;
    see docs/_changelog/CRA-209-tune-wiring.md) so editing the search space changes the sha,
    exactly like editing any other knob. It carries **no** free model text and never reads a
    fluid value: the security boundary is upheld because a knob *domain* is author config, not
    session data.
    """

    model_config = {"frozen": True}

    knobs: list[KnobDomain] = Field(default_factory=list)

    def named_knobs(self) -> Iterator[tuple[str, KnobDomain]]:
        """Yield ``(path, domain)`` for every **tunable** knob, sorted by path.

        Pinned (``tunable=False``) knobs are skipped — they are declared but immovable.
        Path-sorted so enumeration is stable and free of dict/insertion-order leakage (the
        same determinism contract the mutators hold).
        """
        for domain in sorted(self.knobs, key=lambda k: k.path):
            if domain.tunable:
                yield domain.path, domain

    def is_tunable(self, path: str) -> bool:
        """True iff ``path`` is declared **and** tunable. Unknown paths are not tunable."""
        for domain in self.knobs:
            if domain.path == path:
                return domain.tunable
        return False

    # -- tune.toml round-trip ------------------------------------------------
    @classmethod
    def from_toml(cls, text: str) -> TuneSpec:
        """Parse a ``tune.toml`` document into a :class:`TuneSpec`.

        Authoring shape (array-of-tables, stable + diffable)::

            [[knob]]
            path = "agent.worker.model"
            values = ["fast", "mid", "slow"]
            tunable = true
        """
        data = tomllib.loads(text)
        raw = data.get("knob", [])
        knobs = [KnobDomain(**entry) for entry in raw] if isinstance(raw, list) else []
        return cls(knobs=knobs)

    def to_dict(self) -> dict[str, object]:
        """The canonical, JSON-ready payload (path-sorted) for export + hashing.

        Used by :func:`tune_spec_sha` and by ``Definition.content_dict()``: a stable dict that
        round-trips and whose edit perturbs the sha.
        """
        return {
            "knobs": [
                domain.model_dump(mode="json")
                for domain in sorted(self.knobs, key=lambda k: k.path)
            ]
        }


def tune_spec_sha(spec: TuneSpec) -> str:
    """Deterministic 12-char content hash of a :class:`TuneSpec`.

    The seam for folding the tune-spec into a Definition's content identity:
    ``Definition.content_dict()`` folds this in (only when the spec is non-empty) so editing the
    search space changes the sha. An empty spec hashes to a stable constant — but a tune-less
    Definition OMITS the key entirely (see ``Definition.content_dict``), so adding an *empty*
    ``tune.toml`` is hash-neutral.
    """
    blob = json.dumps(spec.to_dict(), sort_keys=True, default=str).encode()
    return hashlib.sha256(blob).hexdigest()[:12]
