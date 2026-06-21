"""Coverage gate for the explained reference docs.

Loads ``crawfish.__all__`` (the authoritative public surface) and verifies that
every public symbol is mentioned by name on at least one ``docs/reference/*.md``
page. Fails — non-zero exit, names listed — if any symbol is undocumented.

Usage::

    uv run python scripts/check_docs_coverage.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import crawfish

REFERENCE_DIR = Path(__file__).resolve().parent.parent / "docs" / "reference"


def public_symbols() -> list[str]:
    return [n for n in crawfish.__all__ if n != "__version__"]


def documented_symbols() -> set[str]:
    """Every identifier that appears as a whole word in any reference page."""
    found: set[str] = set()
    for md in REFERENCE_DIR.glob("*.md"):
        text = md.read_text(encoding="utf-8")
        found.update(re.findall(r"\b[A-Za-z_][A-Za-z0-9_]*\b", text))
    return found


def main() -> int:
    if not REFERENCE_DIR.is_dir():
        print(f"FAIL: {REFERENCE_DIR} does not exist", file=sys.stderr)
        return 1

    symbols = public_symbols()
    documented = documented_symbols()
    missing = sorted(s for s in symbols if s not in documented)

    print(f"Public symbols:     {len(symbols)}")
    print(f"Documented symbols: {len(symbols) - len(missing)}")
    if missing:
        print(f"\nMISSING ({len(missing)}):")
        for name in missing:
            print(f"  - {name}")
        return 1
    print("\nCoverage: 100% — every public symbol is documented.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
