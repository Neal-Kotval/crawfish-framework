"""CRA-251 acceptance: the plugin's slash-command wrappers over ``craw code`` verbs.

The wrappers are *ergonomics over the CLI* — the CLI stays the one execution path. Each is
a thin ``craw code <verb> --json`` shell-out with no embedded logic. Side-effecting verbs
(``init``/``new``) are user-only (``disable-model-invocation: true``); read-only verbs are
not. Deterministic, structural checks only.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

PLUGIN_DIR = Path(__file__).resolve().parents[1] / "src" / "crawfish" / "plugin"
COMMANDS_DIR = PLUGIN_DIR / "commands"

# verb -> (command filename, side-effecting?)
VERBS = {
    "init": ("craw-init.md", True),
    "new": ("craw-new.md", True),
    "sync": ("craw-sync.md", False),
    "map": ("craw-map.md", False),
    "describe": ("craw-describe.md", False),
    "eval": ("craw-eval.md", False),
}


def _split_front_matter(path: Path) -> tuple[dict[str, object], str]:
    text = path.read_text()
    assert text.startswith("---\n"), f"{path} must open with YAML front-matter"
    _, fm, body = text.split("---\n", 2)
    data = yaml.safe_load(fm)
    assert isinstance(data, dict)
    return data, body


@pytest.mark.parametrize("verb", sorted(VERBS), ids=sorted(VERBS))
def test_every_verb_has_a_wrapper(verb: str) -> None:
    filename, _ = VERBS[verb]
    assert (COMMANDS_DIR / filename).is_file()


@pytest.mark.parametrize("verb", sorted(VERBS), ids=sorted(VERBS))
def test_wrapper_name_is_crawfish_prefixed(verb: str) -> None:
    filename, _ = VERBS[verb]
    fm, _ = _split_front_matter(COMMANDS_DIR / filename)
    assert str(fm["name"]).startswith("crawfish-")  # reserved prefix, no export collision


@pytest.mark.parametrize("verb", sorted(VERBS), ids=sorted(VERBS))
def test_side_effecting_wrappers_are_model_disabled(verb: str) -> None:
    filename, side_effecting = VERBS[verb]
    fm, _ = _split_front_matter(COMMANDS_DIR / filename)
    if side_effecting:
        assert fm.get("disable-model-invocation") is True
    else:
        assert fm.get("disable-model-invocation") in (None, False)


@pytest.mark.parametrize("verb", sorted(VERBS), ids=sorted(VERBS))
def test_wrapper_shells_out_to_the_verb_with_json(verb: str) -> None:
    filename, _ = VERBS[verb]
    _, body = _split_front_matter(COMMANDS_DIR / filename)
    assert f"craw code {verb}" in body
    assert "--json" in body
    # No embedded execution logic — a wrapper invokes one verb, not a second craw subtree.
    other_verbs = {f"craw code {v}" for v in VERBS if v != verb}
    for other in other_verbs:
        assert other not in body


def test_no_stray_command_files() -> None:
    """Only the wrappers we declare exist (no unprefixed / orphan command leaks)."""
    present = {p.name for p in COMMANDS_DIR.glob("*.md")}
    expected = {filename for filename, _ in VERBS.values()}
    assert present == expected
