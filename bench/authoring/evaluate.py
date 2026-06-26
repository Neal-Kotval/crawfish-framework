#!/usr/bin/env python3
"""Score authoring-benchmark outputs against the REAL craw pipeline.

Usage: python3 bench/authoring/evaluate.py bench/authoring/.out

For every authored ``definitions/<name>/`` under the output tree, reports:
  compiles      — loads under the jail (load_definition_jailed, denied=False)
  gate          — passes the ALG-3 assembly gate (assert_build_safe)
  sink_target   — every consequential sink-target-shaped output is Flow.STATIC
                  (heuristic: an output whose name looks like a destination/target/key)
  flows         — the input/output Flow tags
"""

from __future__ import annotations

import sys
from pathlib import Path

from crawfish.build import assert_build_safe
from crawfish.definition.jailed import load_definition_jailed
from crawfish.jail import SandboxPolicy
from crawfish.store import SqliteStore

# output names that denote a consequential sink TARGET (where a write lands), not content
TARGETISH = ("channel", "target", "to", "recipient", "url", "endpoint", "dest", "key", "id")


def score(defn_dir: Path) -> dict[str, object]:
    try:
        r = load_definition_jailed(
            defn_dir, store=SqliteStore(), org_id="local", policy=SandboxPolicy(kind="fake")
        )
    except Exception as e:  # compile/jail failure
        return {"compiles": False, "error": f"{type(e).__name__}: {e}"[:90]}
    d = r.definition
    ins = [(p.name, p.flow.value) for p in d.inputs]
    outs = [(p.name, p.flow.value) for p in d.outputs]
    try:
        assert_build_safe([d])
        gate = "pass"
    except Exception as e:
        gate = f"reject({type(e).__name__})"
    targets = [p for p in d.outputs if any(t in p.name.lower() for t in TARGETISH)]
    sink_safe = all(p.flow.value == "static" for p in targets) if targets else None
    return {"compiles": True, "gate": gate, "sink_target_safe": sink_safe,
            "in": ins, "out": outs}


def main(root: str) -> None:
    base = Path(root)
    dirs = sorted({p.parent for p in base.rglob("definitions/*/definition.py")})
    if not dirs:
        print(f"no authored definitions under {root}")
        return
    for d in dirs:
        rel = d.relative_to(base)
        s = score(d)
        if not s["compiles"]:
            print(f"{str(rel):40} compiles=FAIL  {s['error']}")
            continue
        print(f"{str(rel):40} compiles=OK gate={s['gate']:22} sink_target_safe={s['sink_target_safe']}")
        print(f"{'':40}   in ={s['in']}")
        print(f"{'':40}   out={s['out']}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "bench/authoring/.out")
