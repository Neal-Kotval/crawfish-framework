"""CRA-131: manifest parsing + profile resolution (dev→command, prod→managed).

CRA-192: ``[models]`` parsing → ModelsConfig (default, aliases, allowed-providers)
and alias→alias rejection at load time.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from crawfish.config import (
    ModelsConfigError,
    load_manifest,
    load_models_config,
)


def test_defaults_when_no_manifest(tmp_path: Path) -> None:
    m = load_manifest(tmp_path)
    assert m.resolve_profile("dev").runtime == "command"
    assert m.resolve_profile("prod").runtime == "managed"


def test_manifest_overrides(tmp_path: Path) -> None:
    (tmp_path / "crawfish.toml").write_text(
        """
[project]
name = "triage-bot"
default_profile = "dev"

[profiles.dev]
runtime = "command"

[profiles.staging]
runtime = "client"
"""
    )
    m = load_manifest(tmp_path)
    assert m.name == "triage-bot"
    assert m.resolve_profile().runtime == "command"  # default_profile
    assert m.resolve_profile("staging").runtime == "client"


def test_unknown_profile_raises(tmp_path: Path) -> None:
    with pytest.raises(KeyError):
        load_manifest(tmp_path).resolve_profile("nope")


# -- CRA-192: [models] section --------------------------------------------------
def _write(tmp_path: Path, body: str) -> Path:
    (tmp_path / "crawfish.toml").write_text(body)
    return tmp_path


def test_models_section_parses_default_and_aliases(tmp_path: Path) -> None:
    _write(
        tmp_path,
        """
[project]
name = "p"

[models]
default = "claude-sonnet-4-6"
allowed_providers = ["anthropic", "openai"]

[models.aliases]
fast = "claude-haiku-4-5"
cheap = "openai:gpt-4o-mini"
""",
    )
    cfg = load_models_config(tmp_path)
    assert cfg.default == "claude-sonnet-4-6"
    assert cfg.aliases == {"fast": "claude-haiku-4-5", "cheap": "openai:gpt-4o-mini"}
    assert cfg.policy.allowed == ("anthropic", "openai")
    # ModelsConfig is frozen.
    with pytest.raises(Exception):  # noqa: B017 - pydantic ValidationError on frozen
        cfg.default = "x"  # type: ignore[misc]
    # Surfaced on the manifest too.
    assert load_manifest(tmp_path).models.default == "claude-sonnet-4-6"


def test_no_models_section_is_empty_config(tmp_path: Path) -> None:
    _write(tmp_path, '[project]\nname = "p"\n')
    cfg = load_models_config(tmp_path)
    assert cfg.default is None
    assert cfg.aliases == {}
    assert cfg.policy.allowed is None


def test_no_manifest_file_is_empty_config(tmp_path: Path) -> None:
    cfg = load_models_config(tmp_path)
    assert cfg.default is None and cfg.aliases == {}


def test_alias_to_alias_chain_rejected_at_load(tmp_path: Path) -> None:
    _write(
        tmp_path,
        """
[project]
name = "p"

[models.aliases]
fast = "claude-haiku-4-5"
turbo = "fast"
""",
    )
    with pytest.raises(ModelsConfigError, match="another alias"):
        load_models_config(tmp_path)
    # The full manifest load enforces it too.
    with pytest.raises(ModelsConfigError):
        load_manifest(tmp_path)


def test_bad_default_type_rejected(tmp_path: Path) -> None:
    _write(tmp_path, "[models]\ndefault = 123\n")
    with pytest.raises(ModelsConfigError):
        load_models_config(tmp_path)
