---
name: crawfish-authoring-tools-py
description: >
  Author tools/*.py — a callable whose name matches the filename stem, with typed IO and
  taint propagation from fluid inputs. Load when writing a host-side tool. Agent-authored
  tool code compiles jailed (CRA-267), fail-closed.
user-invocable: false
allowed-tools: Read, Grep
---

# Authoring `tools/*.py`

Derived from `docs/specs/craw-code/authoring/tools-py.md`. Golden:
`demo/craw-code-golden/tools/normalize_ticket.py`.

Each `tools/*.py` defines **one callable whose name equals the filename stem**. The compiler
discovers `tools/normalize_ticket.py` and requires a callable named `normalize_ticket`; a
mismatch fails at load with `DefinitionLoadError`. A leading-underscore file (`_helpers.py`)
is a private helper, not a tool.

```python
"""normalize_ticket — the callable name MUST equal the filename stem."""
from __future__ import annotations


def normalize_ticket(ticket_body: str) -> str:
    """Lower-case + strip a ticket body. The argument is FLUID (untrusted) data."""
    return ticket_body.strip().lower()
```

## Taint propagates from fluid inputs

Host-side tool code runs **out-of-process at run time**. Treat every tool argument fed from a
fluid input as untrusted; never return a value into a consequential static slot.

> **Spine rule (taint-propagates):** A value derived from a fluid input stays tainted and
> never becomes a static sink target.

## Agent-authored tools compile jailed

Under `craw code` the author may be the agent, so `tools/*.py` is import-bearing code the
compiler executes. It compiles through the jailed path (`load_definition_jailed`): the
project dir is read-only, the network is denied, and any folder-escape or egress attempt is
refused — the authored code never runs in the orchestrator. A compile that read fluid-derived
files comes back tainted on the file's provenance row.

> **Spine rule (jailed-compile):** Agent-authored import-bearing code compiles through the
> jail (CRA-267), fail-closed.

(A human-authored tool keeps the fast in-process path; the jail is for the agent-authored, or
unknown-authorship, case.)
