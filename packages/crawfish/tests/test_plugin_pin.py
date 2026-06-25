"""UNFILED-PIN acceptance: the plugin bundle is pinned, integrity-checked, version-ranged.

The plugin is the *source of the security rules* an agent reads; an unpinned bundle can be
silently swapped. These tests cover the pin helper (:mod:`crawfish.code.plugin`): a
deterministic digest, the pin written by ``craw code init``, ``craw doctor`` flagging a
tampered bundle, and ``craw code sync`` failing closed on an incompatible ``requires_crawfish``
range. Deterministic — tmp dirs, no network, no model.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from crawfish.code.cli import run_code
from crawfish.code.plugin import (
    PLUGIN_PIN_FILE,
    BundleMismatch,
    PluginPin,
    bundle_digest,
    compute_pin,
    read_manifest,
    read_pin,
    requires_satisfied_by,
    verify_bundle,
    write_pin,
)
from crawfish.doctor import diagnose

BUNDLE = Path(__file__).resolve().parents[1] / "src" / "crawfish" / "plugin"


# --------------------------------------------------------------------------- digest


def test_bundle_digest_is_deterministic() -> None:
    """Identical inputs ⇒ identical digest across two computations (sorted-file, stable)."""
    assert bundle_digest(BUNDLE) == bundle_digest(BUNDLE)
    assert bundle_digest(BUNDLE).startswith("sha256:")


def test_bundle_digest_changes_when_a_skill_changes(tmp_path: Path) -> None:
    import shutil

    a = tmp_path / "a"
    b = tmp_path / "b"
    shutil.copytree(BUNDLE, a)
    shutil.copytree(BUNDLE, b)
    assert bundle_digest(a) == bundle_digest(b)
    # mutate one skill in b → digest diverges
    skill = b / "skills" / "crawfish-security-spine" / "SKILL.md"
    skill.write_text(skill.read_text() + "\n<!-- tampered -->\n")
    assert bundle_digest(a) != bundle_digest(b)


def test_digest_ignores_pycache(tmp_path: Path) -> None:
    import shutil

    a = tmp_path / "a"
    shutil.copytree(BUNDLE, a)
    before = bundle_digest(a)
    (a / "__pycache__").mkdir()
    (a / "__pycache__" / "junk.pyc").write_bytes(b"\x00\x01")
    assert bundle_digest(a) == before  # generated paths excluded


# --------------------------------------------------------------------------- manifest + pin


def test_manifest_carries_version_and_requires_range() -> None:
    manifest = read_manifest(BUNDLE)
    assert manifest.name == "crawfish"
    assert manifest.requires_crawfish  # a non-empty compat range
    assert manifest.version


def test_compute_pin_matches_manifest_and_digest() -> None:
    pin = compute_pin(BUNDLE)
    manifest = read_manifest(BUNDLE)
    assert pin.bundle_sha256 == bundle_digest(BUNDLE)
    assert pin.requires_crawfish == manifest.requires_crawfish
    assert pin.version == manifest.version


def test_pin_roundtrips_through_file(tmp_path: Path) -> None:
    pin = compute_pin(BUNDLE)
    write_pin(pin, tmp_path)
    assert (tmp_path / PLUGIN_PIN_FILE).is_file()
    assert read_pin(tmp_path) == pin


def test_pin_file_is_the_spec_fragment_shape(tmp_path: Path) -> None:
    """The serialized pin is the spec's ``{ "plugin": {...} }`` fragment (just a new file)."""
    pin = compute_pin(BUNDLE)
    write_pin(pin, tmp_path)
    data = json.loads((tmp_path / PLUGIN_PIN_FILE).read_text())
    assert set(data) == {"plugin"}
    assert set(data["plugin"]) == {"name", "version", "bundle_sha256", "requires_crawfish"}


# --------------------------------------------------------------------------- verify (tamper)


def test_verify_bundle_passes_for_matching_digest(tmp_path: Path) -> None:
    import shutil

    dest = tmp_path / "plugins" / "crawfish"
    shutil.copytree(BUNDLE, dest)
    verify_bundle(dest, compute_pin(BUNDLE))  # no raise


def test_verify_bundle_fails_closed_on_tamper(tmp_path: Path) -> None:
    import shutil

    dest = tmp_path / "plugins" / "crawfish"
    shutil.copytree(BUNDLE, dest)
    pin = compute_pin(dest)
    skill = dest / "skills" / "crawfish-security-spine" / "SKILL.md"
    skill.write_text(skill.read_text() + "\n<!-- swapped rules -->\n")
    with pytest.raises(BundleMismatch):
        verify_bundle(dest, pin)


# --------------------------------------------------------------------------- requires range


@pytest.mark.parametrize(
    ("range_str", "version", "ok"),
    [
        (">=0.3,<0.4", "0.3.0", True),
        (">=0.3,<0.4", "0.3.9", True),
        (">=0.3,<0.4", "0.4.0", False),
        (">=0.3,<0.4", "0.2.9", False),
        (">=0.2,<0.3", "0.3.0", False),
        ("", "9.9.9", True),  # empty range == unconstrained
        ("==0.3.0", "0.3.0", True),
        ("==0.3.0", "0.3.1", False),
    ],
)
def test_requires_satisfied_by(range_str: str, version: str, ok: bool) -> None:
    assert requires_satisfied_by(range_str, version) is ok


def test_requires_invalid_clause_fails_closed() -> None:
    from crawfish.resolve import ResolutionError

    with pytest.raises(ResolutionError):
        requires_satisfied_by(">=not.a.version", "0.3.0")


# --------------------------------------------------------------------------- init writes pin


def test_init_writes_the_pin(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    app = tmp_path / "app"
    rc = run_code(["init", str(app), "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    plugin = payload["plugin"]
    assert plugin["installed"] is True
    assert plugin["bundle_sha256"].startswith("sha256:")
    assert plugin["requires_crawfish"]
    # the pin file is written and matches the installed bundle
    pin = read_pin(app)
    assert pin is not None
    verify_bundle(app / ".claude" / "plugins" / "crawfish", pin)


def test_init_no_plugin_writes_no_pin(tmp_path: Path) -> None:
    app = tmp_path / "app"
    rc = run_code(["init", str(app), "--no-plugin"])
    assert rc == 0
    assert read_pin(app) is None
    assert not (app / PLUGIN_PIN_FILE).exists()


# --------------------------------------------------------------------------- doctor + sync


def test_doctor_flags_a_tampered_bundle(tmp_path: Path) -> None:
    app = tmp_path / "app"
    assert run_code(["init", str(app)]) == 0
    bundle = app / ".claude" / "plugins" / "crawfish"
    skill = bundle / "skills" / "crawfish-security-spine" / "SKILL.md"
    skill.write_text(skill.read_text() + "\n<!-- tampered -->\n")
    report = diagnose(app)
    assert any(f.level == "error" and "digest mismatch" in f.message for f in report.findings)


def test_doctor_clean_bundle_reports_ok(tmp_path: Path) -> None:
    app = tmp_path / "app"
    assert run_code(["init", str(app)]) == 0
    report = diagnose(app)
    assert any("matches its pinned digest" in f.message for f in report.findings)


def test_sync_fails_closed_on_incompatible_range(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A pinned bundle whose requires_crawfish excludes the installed version → plugin_skew."""
    app = tmp_path / "app"
    assert run_code(["init", str(app)]) == 0
    # rewrite the pin with a range no installed version can satisfy (fail-closed precondition)
    pin = read_pin(app)
    assert pin is not None
    skewed = PluginPin(
        name=pin.name,
        version=pin.version,
        bundle_sha256=pin.bundle_sha256,
        requires_crawfish=">=99,<100",
    )
    write_pin(skewed, app)
    capsys.readouterr()  # drain
    rc = run_code(["sync", "--dir", str(app), "--json"])
    assert rc == 1  # EXIT_EXPECTED_FAILURE
    err = capsys.readouterr().err.strip().splitlines()[-1]
    payload = json.loads(err)
    assert payload["code"] == "plugin_skew"
