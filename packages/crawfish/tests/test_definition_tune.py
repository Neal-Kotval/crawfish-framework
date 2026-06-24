"""CRA-209 (AL-T1) acceptance: ``Definition.tune`` round-trips and folds into the
content hash — hash-neutral when empty.

The load-bearing rule (see docs/_changelog/CRA-209-tune-wiring.md):

* a tune-less Definition hashes EXACTLY as before (the ``tune`` key is omitted), so
  adding the optional field perturbs no pre-existing frozen artifact;
* a non-empty tune folds ``tune_spec_sha`` into the content sha (tuning is a content
  change) and round-trips through ``export()``;
* a ``tune.toml`` authored in the directory lands in ``Definition.tune``.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from crawfish.definition import Definition, load_definition
from crawfish.tune import KnobDomain, TuneSpec

FIXTURES = Path(__file__).parent / "fixtures"


def _copy(name: str, dest: Path) -> Path:
    target = dest / name
    shutil.copytree(FIXTURES / name, target)
    return target


def _base() -> Definition:
    """A small, deterministic Definition built directly (no directory needed)."""
    from crawfish.definition.types import AgentSpec, TeamSpec

    return Definition(
        id="tune-fixture",
        team=TeamSpec(agents=[AgentSpec(role="main", prompt="do the thing")]),
    )


def test_tuneless_definition_hash_is_neutral() -> None:
    """A Definition with no tune hashes identically to one whose ``tune`` is dropped.

    The pre-change content_dict had no ``tune`` key at all; the post-change one omits it
    when tune is empty. So the canonical payload — and the sha — must be byte-identical
    to a payload that never knew about ``tune``.
    """
    d = _base()
    assert d.tune is None

    payload = d.content_dict()
    assert "tune" not in payload  # omitted entirely — the hash-neutral guarantee

    # An explicitly-empty TuneSpec is ALSO hash-neutral (no knobs to search).
    d_empty = _base()
    d_empty.tune = TuneSpec(knobs=[])
    assert "tune" not in d_empty.content_dict()
    assert d_empty.content_sha() == d.content_sha()


def test_nonempty_tune_changes_sha_and_round_trips() -> None:
    """A non-empty tune diverges the sha and survives an ``export()`` round-trip."""
    d = _base()
    base_sha = d.content_sha()

    spec = TuneSpec(knobs=[KnobDomain(path="agent.main.model", values=["fast", "slow"])])
    d.tune = spec

    # Folding a non-empty tune is a real content change.
    assert "tune" in d.content_dict()
    assert d.content_sha() != base_sha

    # Editing the search space changes the sha again (versions the agent).
    d2 = _base()
    d2.tune = TuneSpec(knobs=[KnobDomain(path="agent.main.model", values=["fast", "slow", "mid"])])
    assert d2.content_sha() != d.content_sha()

    # export() serializes the TuneSpec and reloads it losslessly.
    pkg = d.export()
    reloaded = Definition(**pkg.definition)
    assert reloaded.tune is not None
    assert reloaded.tune == spec


def test_tuneless_export_round_trips_to_none() -> None:
    """A tune-less Definition exports and reloads with ``tune is None``."""
    d = _base()
    pkg = d.export()
    reloaded = Definition(**pkg.definition)
    assert reloaded.tune is None


def test_tune_toml_lands_in_definition(tmp_path: Path) -> None:
    """A ``tune.toml`` authored in the directory compiles into ``Definition.tune``."""
    root = _copy("minimal", tmp_path)
    (root / "tune.toml").write_text(
        '[[knob]]\npath = "agent.main.model"\nvalues = ["fast", "slow"]\ntunable = true\n'
    )
    d = load_definition(root)
    assert d.tune is not None
    paths = [k.path for k in d.tune.knobs]
    assert paths == ["agent.main.model"]


def test_empty_tune_toml_stays_tuneless(tmp_path: Path) -> None:
    """An empty ``tune.toml`` (no ``[[knob]]`` tables) leaves the Definition tune-less.

    This keeps the directory-derived version sha hash-neutral: authoring an empty
    tune.toml must NOT mint a new content identity.
    """
    root = _copy("minimal", tmp_path)
    (root / "tune.toml").write_text("# no knobs yet\n")
    d = load_definition(root)
    assert d.tune is None
    assert "tune" not in d.content_dict()
