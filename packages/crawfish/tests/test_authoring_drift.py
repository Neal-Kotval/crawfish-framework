"""CRA-256 headline guard — the per-file authoring skills are DERIVED, not duplicated.

The authoring spec's central promise is "teaching and checking read ONE source": each
per-file plugin skill (``crawfish-authoring-*``) is derived from its section in
``authoring-spec.toml`` + ``docs/specs/craw-code/authoring/<file>.md``. These tests pin that
promise mechanically — a skill cannot silently drift away from the spec:

* every ``[[file]]`` entry's skill exists as a discoverable ``SKILL.md`` with valid
  front-matter (``name`` matches the slug; ``user-invocable: false`` — background knowledge);
* every spine rule the entry ``requires_spine`` appears **verbatim** in that skill's body
  (whitespace-normalized so a markdown blockquote line-wrap still matches), so the skill
  quotes the canonical rule rather than paraphrasing it.

Pure filesystem + TOML + front-matter parse. No model call.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest
import yaml

_REPO = Path(__file__).resolve().parents[3]
_SPEC_DIR = _REPO / "docs" / "specs" / "craw-code" / "authoring"
_SPEC_TOML = _SPEC_DIR / "authoring-spec.toml"
_SKILLS_DIR = _REPO / "packages" / "crawfish" / "src" / "crawfish" / "plugin" / "skills"


def _spec() -> dict:
    return tomllib.loads(_SPEC_TOML.read_text())


def _norm(text: str) -> str:
    """Collapse whitespace and strip markdown blockquote markers for substring matching."""
    return re.sub(r"\s+", " ", text.replace(">", " ")).strip()


def _front_matter(skill_md: Path) -> tuple[dict, str]:
    text = skill_md.read_text()
    assert text.startswith("---"), f"{skill_md} missing YAML front-matter"
    end = text.find("\n---", 3)
    assert end != -1, f"{skill_md} front-matter not terminated"
    fm = yaml.safe_load(text[3:end]) or {}
    body = text[end + 4 :]
    return fm, body


def _file_entries() -> list[dict]:
    return _spec()["file"]


def test_every_file_skill_exists_and_is_discoverable() -> None:
    """Each [[file]] entry's skill is a real ``skills/<slug>/SKILL.md``."""
    for entry in _file_entries():
        skill_md = _SKILLS_DIR / entry["skill"] / "SKILL.md"
        assert skill_md.is_file(), f"{entry['kind']} → missing skill {entry['skill']}"


@pytest.mark.parametrize("entry", _file_entries(), ids=lambda e: e["skill"])
def test_skill_front_matter(entry: dict) -> None:
    """Front-matter: ``name`` matches the slug; the skill is background (user-invocable false)."""
    fm, _ = _front_matter(_SKILLS_DIR / entry["skill"] / "SKILL.md")
    assert fm.get("name") == entry["skill"]
    assert fm.get("user-invocable") is False
    # A knowledge skill is read-only: no Write/Bash side effects (Bash allowed where the
    # section runs a craw CLI read — fixtures/evals).
    tools = str(fm.get("allowed-tools", ""))
    assert "Write" not in tools


@pytest.mark.parametrize("entry", _file_entries(), ids=lambda e: e["skill"])
def test_skill_embeds_canonical_spine_rules(entry: dict) -> None:
    """No-drift: every required spine rule appears verbatim in the skill body.

    The skill quotes the canonical ``rule`` string from ``authoring-spec.toml`` — the single
    source — so it cannot paraphrase its way out of the spec. Whitespace-normalized so a
    blockquote line-wrap still counts as a match.
    """
    spine = _spec()["spine"]
    _, body = _front_matter(_SKILLS_DIR / entry["skill"] / "SKILL.md")
    norm_body = _norm(body)
    for tag in entry.get("requires_spine", []):
        assert tag in spine, f"{entry['skill']} requires unknown spine tag {tag!r}"
        rule = _norm(spine[tag]["rule"])
        assert rule in norm_body, (
            f"skill {entry['skill']} has drifted: it must quote the canonical {tag!r} rule "
            f"verbatim — expected {spine[tag]['rule']!r}"
        )


def test_optimizing_skill_present_and_background() -> None:
    """UNFILED-OPT ships as a background authoring skill (no [[file]] entry — not a file kind)."""
    fm, body = _front_matter(_SKILLS_DIR / "crawfish-authoring-optimizing" / "SKILL.md")
    assert fm.get("name") == "crawfish-authoring-optimizing"
    assert fm.get("user-invocable") is False
    low = body.lower()
    assert "train" in low and "eval" in low  # train vs eval mode
    assert "--budget" in body  # every example budget-bounded
    assert "--seed" in body  # determinism
