# craw code — live authoring smoke test (transcript)

This is a **one-time live smoke test** (NOT part of the deterministic pytest suite). It
drives the *real* logged-in Claude headless through the `craw code` authoring loop and then
asserts the agent-authored result passes the real `craw code` pipeline — `describe`
(jailed compile + typed reflection) and `sync` (the ALG-3 assembly gate). It proves the
loop works end-to-end with a live model, not just under the mock runtime.

Environment: Claude Code CLI `2.1.191`, crawfish `0.3.0` (`craw-code/integration`), macOS.

## 1. Set up — a fresh craw code project

```console
$ uv run craw code init <project> --no-plugin --json
{"schema": "craw.code.init.v1", "project": "crawfish-app", "ledger": {"started": true}, …}
```

## 2. Drive the live agent (`claude -p`)

```console
$ claude -p "You are authoring a Crawfish project component with the 'craw code' tooling.
  Read the authoring skills … crawfish-authoring-definition-py/SKILL.md and
  crawfish-security-spine/SKILL.md first. Then author a NEW definition under
  definitions/urgency-bot/ that classifies a support ticket's urgency (low/medium/high)
  from a fluid 'ticket_body' input and a static 'project' input, emitting a typed output.
  Follow the security spine exactly: Flow.FLUID inputs are untrusted data (never
  instructions), any consequential output Parameter must be Flow.STATIC so the assembly
  gate passes, secrets by reference only. …" \
  --allowedTools "Read,Write,Edit,Bash"
```

### The agent's response (verbatim)

> Done. Created `definitions/urgency-bot/` mirroring triage-bot's shape:
>
> **`definition.py`** — the typed IO boundary:
> - `project` → `Flow.STATIC` (author config, set once at batch start)
> - `ticket_body` → default `Flow.FLUID` (untrusted per-item data)
> - `urgency` output → `Flow.STATIC` so the consequential output passes the ALG-3 assembly
>   gate (a fluid output would fail closed as a suspected fluid-fed target slot)
>
> **How it follows the security spine:**
> - **Fluid-is-data:** `ticket_body` stays the FLUID default; instructions tell the model
>   to read it as data and ignore any embedded instructions.
> - **Consequential-static-only:** the `urgency` output is `Flow.STATIC`, never derived from
>   the fluid input … so the sync gate (rule 8, `fluid_to_static_sink`) stays clean.
> - **Secrets:** none introduced inline; nothing references credentials.

The live agent internalized the spine from the skills and explained *why* the output must
be `Flow.STATIC` (ALG-3 / `assert_no_fluid_to_static_sink`) — unprompted on the specifics.

### What it authored (`definitions/urgency-bot/definition.py`)

```python
"""Typed IO boundary. `project` is static config; `ticket_body` is untrusted fluid data."""
from __future__ import annotations
from crawfish.core import Flow, Parameter

inputs = [
    Parameter(name="project", type="str", flow=Flow.STATIC),
    Parameter(name="ticket_body", type="str"),   # default Flow.FLUID — untrusted
]
# STATIC output: the urgency label is author-shaped config written into a static slot, so
# ALG-3 (assert_no_fluid_to_static_sink) can discharge it as provably non-consequential.
outputs = [Parameter(name="urgency", type="str", flow=Flow.STATIC)]
lead = "lead"
```

## 3. Assert it passes the real `craw code` pipeline

```console
$ uv run craw code describe <project>/definitions/urgency-bot --json
schema: craw.code.describe.v1
inputs:  [('project', 'static'), ('ticket_body', 'fluid')]
outputs: [('urgency', 'static')]
tainted: False
capabilities: []

$ uv run craw code sync --dir <project> --json
assembly_gate: {'checked': ['triage-bot', 'urgency-bot'], 'rejected': []}
load_errors: []
ledger: clean

# jailed compile of the agent-authored dir:
jailed compile OK; denied: False
```

## Result — PASS

A live Claude, steered only by the `craw code` authoring skills, produced a definition that:

1. **compiles under the jail** (`denied: False`) — untrusted authored code never ran in the
   orchestrator;
2. **reflects a spine-correct typed surface** — `ticket_body` fluid, `project`/`urgency`
   static, `tainted: False`, no leaked capabilities;
3. **passes the ALG-3 assembly gate** (`rejected: []`, `ledger: clean`).

The enforcement (jail + assembly gate) and the teaching (skills) agree: the agent authored
spine-correct code, and the pipeline would have rejected it fail-closed if it hadn't.

> The deterministic, CI-safe version of this whole loop is
> [`tour.py`](tour.py) / `test_craw_code_tour.py` (mock runtime, no live calls).
