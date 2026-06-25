"""CRA-248/249/250 acceptance: the plugin's knowledge skills (the teaching half of the spine).

Deterministic, structural assertions only — parse each ``SKILL.md``'s YAML front-matter and
markdown body. No live model call, no network. These verify the skills *teach the spine
accurately*; the enforcement is tested separately (assembly gate, consent re-gate, lint).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

PLUGIN_DIR = Path(__file__).resolve().parents[1] / "src" / "crawfish" / "plugin"
SKILLS_DIR = PLUGIN_DIR / "skills"

SECURITY_SPINE = SKILLS_DIR / "crawfish-security-spine" / "SKILL.md"
PIPELINE_MODEL = SKILLS_DIR / "crawfish-pipeline-model" / "SKILL.md"
DETERMINISM = SKILLS_DIR / "crawfish-determinism-ledger" / "SKILL.md"

KNOWLEDGE_SKILLS = [SECURITY_SPINE, PIPELINE_MODEL, DETERMINISM]


def _split_front_matter(path: Path) -> tuple[dict[str, object], str]:
    """Parse a SKILL.md into (front-matter dict, body). Asserts a valid ``---`` block."""
    text = path.read_text()
    assert text.startswith("---\n"), f"{path} must open with a YAML front-matter block"
    _, fm, body = text.split("---\n", 2)
    data = yaml.safe_load(fm)
    assert isinstance(data, dict), f"{path} front-matter is not a mapping"
    return data, body


# --------------------------------------------------------------------------- shared shape


@pytest.mark.parametrize("path", KNOWLEDGE_SKILLS, ids=lambda p: p.parent.name)
def test_frontmatter_required_keys(path: Path) -> None:
    fm, _ = _split_front_matter(path)
    assert fm.get("name") == path.parent.name  # name matches the skill dir (CC convention)
    assert isinstance(fm.get("description"), str) and fm["description"].strip()
    assert "allowed-tools" in fm


@pytest.mark.parametrize("path", KNOWLEDGE_SKILLS, ids=lambda p: p.parent.name)
def test_body_under_500_lines(path: Path) -> None:
    _, body = _split_front_matter(path)
    assert len(body.splitlines()) < 500, f"{path} body must stay under 500 lines"


@pytest.mark.parametrize("path", KNOWLEDGE_SKILLS, ids=lambda p: p.parent.name)
def test_knowledge_skills_have_no_write_or_exec_tools(path: Path) -> None:
    """A knowledge skill never grants Write/Edit; only determinism may read via Bash."""
    fm, _ = _split_front_matter(path)
    tools = {t.strip() for t in str(fm["allowed-tools"]).split(",")}
    assert "Write" not in tools and "Edit" not in tools
    if path != DETERMINISM:
        # security-spine and pipeline-model are pure Read/Grep (no Bash side channel).
        assert tools <= {"Read", "Grep"}


# --------------------------------------------------------------------------- CRA-248 spine


def test_security_spine_description_names_the_boundary() -> None:
    fm, _ = _split_front_matter(SECURITY_SPINE)
    desc = str(fm["description"]).lower()
    assert "prompt-injection boundary" in desc
    # triggers on the surfaces the spine governs
    for surface in ("sink", "mcp", "policy", "pipeline"):
        assert surface in desc


def test_security_spine_cites_all_core_rules_and_language_rules() -> None:
    _, body = _split_front_matter(SECURITY_SPINE)
    # The six core rules (key phrases verbatim from SECURITY.md).
    core_phrases = [
        "Fluid inputs are untrusted session data",
        "Consequential sink targets are static-only",
        "Idempotency keys derive from static config",
        "Secrets are matched to nodes",
        "taint propagates from fluid inputs",
        "supply chain is pinned",
    ]
    for phrase in core_phrases:
        assert phrase in body, f"missing core rule phrase: {phrase!r}"
    # Language-era rules 7-9.
    assert "eval mode" in body
    assert "rejected at assembly time" in body or "assembly time" in body
    assert "Aggregate taint is the union" in body


def test_security_spine_points_at_enforcing_verbs() -> None:
    """The skill must cite the verbs that *enforce* the spine, not the rules alone."""
    _, body = _split_front_matter(SECURITY_SPINE)
    assert "craw code sync" in body  # assembly gate
    assert "craw code grant" in body  # consent re-gate
    assert "craw code lint" in body  # secret-shaped lint
    assert "static-only" in body


# --------------------------------------------------------------------------- CRA-249 model


def test_pipeline_model_covers_all_node_kinds() -> None:
    _, body = _split_front_matter(PIPELINE_MODEL)
    lowered = body.lower()
    for node in ("source", "filter", "batch", "aggregator", "router", "sink"):
        assert node in lowered, f"missing pipeline node kind: {node}"


def test_pipeline_model_has_decision_guide_and_coordination_shapes() -> None:
    _, body = _split_front_matter(PIPELINE_MODEL)
    # fan-out / aggregator / router / refine decision guide
    assert "fan-out" in body.lower()
    assert "refine" in body.lower()
    # coordination shapes
    for shape in ("single", "lead", "sequential"):
        assert shape in body
    # links to the authoring playbook
    assert "crawfish-authoring" in body


# --------------------------------------------------------------------------- CRA-250 ledger


def test_determinism_skill_teaches_promotion_and_cost_band() -> None:
    fm, body = _split_front_matter(DETERMINISM)
    tools = {t.strip() for t in str(fm["allowed-tools"]).split(",")}
    assert "Bash" in tools  # ledger reads are CLI calls
    for token in ("--seed", "--live", "--budget", "worst_case_usd"):
        assert token in body


def test_determinism_skill_teaches_retryable_semantics() -> None:
    _, body = _split_front_matter(DETERMINISM)
    assert "retryable" in body
    assert "false" in body.lower()
    assert "security" in body.lower()


def test_no_example_fires_live_without_budget() -> None:
    """A teaching example must never model ``--live`` without a ``--budget`` on the same line."""
    _, body = _split_front_matter(DETERMINISM)
    for line in body.splitlines():
        if "--live" in line and "`" in line and "--budget" not in line:
            # Allow prose mentioning --live in isolation, but no fenced command example of it.
            assert "craw run" not in line and "craw dev" not in line, (
                f"example fires --live without --budget: {line!r}"
            )
