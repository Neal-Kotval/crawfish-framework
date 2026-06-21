"""Accuracy & Determinism gate for reference examples.

For each docs/reference/*.md page, extract the runnable example (the ```python
blocks under the `## Example` heading), execute it in a clean subprocess, and
compare its stdout against the shown `▶ Output` (the ```text block inside the
`??? success` admonition). Fails loudly on any drift or runtime error.

Determinism is also checked: each example is run TWICE; if the two runs differ,
the example is non-deterministic and is flagged.

Usage::

    uv run python scripts/verify_examples.py
"""

from __future__ import annotations

import re
import subprocess
import sys
import textwrap
from pathlib import Path

REFERENCE_DIR = Path(__file__).resolve().parent.parent / "docs" / "reference"

# Honor real Markdown fence semantics: a closing ``` only closes when it is at the
# start of a line. This ignores mid-line backticks inside Python string literals
# (e.g. a fenced-JSON sample stored in a variable).
PY_BLOCK = re.compile(r"^```python\n(.*?)^```", re.DOTALL | re.MULTILINE)
# The output text block lives inside an indented `??? success` admonition, so its
# fences are indented by 4 spaces.
TEXT_BLOCK = re.compile(r"^ {4}```text\n(.*?)^ {4}```", re.DOTALL | re.MULTILINE)


def example_section(md: str) -> str | None:
    idx = md.find("## Example")
    return md[idx:] if idx != -1 else None


def run(code: str) -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=REFERENCE_DIR.parent.parent,
    )
    return proc.returncode, proc.stdout + proc.stderr


def main() -> int:
    failures: list[str] = []
    checked = 0
    for md_path in sorted(REFERENCE_DIR.glob("*.md")):
        if md_path.name == "index.md":
            continue
        md = md_path.read_text(encoding="utf-8")
        section = example_section(md)
        if section is None:
            failures.append(f"{md_path.name}: no '## Example' section")
            continue
        py_blocks = PY_BLOCK.findall(section)
        if not py_blocks:
            failures.append(f"{md_path.name}: no python block under Example")
            continue
        code = "\n".join(py_blocks)
        # The output block is indented inside the admonition; dedent each line.
        text_blocks = TEXT_BLOCK.findall(section)
        if not text_blocks:
            failures.append(f"{md_path.name}: no '▶ Output' text block")
            continue
        expected = textwrap.dedent(text_blocks[0]).rstrip("\n")

        rc, out = run(code)
        rc2, out2 = run(code)
        actual = out.rstrip("\n")
        checked += 1
        if rc != 0:
            failures.append(
                f"{md_path.name}: example raised (exit {rc}):\n{textwrap.indent(out, '    ')}"
            )
            continue
        if out != out2:
            failures.append(f"{md_path.name}: NON-DETERMINISTIC — two runs differ")
            continue
        # Compare ignoring the admonition's 4-space indentation on output lines.
        exp_norm = "\n".join(line.rstrip() for line in expected.splitlines())
        act_norm = "\n".join(line.rstrip() for line in actual.splitlines())
        if exp_norm != act_norm:
            shown = textwrap.indent(exp_norm, "    ")
            got = textwrap.indent(act_norm, "    ")
            failures.append(
                f"{md_path.name}: OUTPUT DRIFT\n  --- shown ---\n{shown}\n  --- actual ---\n{got}"
            )

    print(f"Examples checked: {checked}")
    if failures:
        print(f"\nFAILURES ({len(failures)}):\n")
        for f in failures:
            print(f"- {f}\n")
        return 1
    print("All examples run clean, deterministic, and match shown output byte-for-byte.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
