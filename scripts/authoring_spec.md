# Reference page authoring spec (for doc subagents)

You are authoring ONE page of the Crawfish explained reference. Match the gold-standard
exemplar **exactly** in structure, depth, length, and tone.

## Before writing
1. **Read the exemplar** `docs/reference/core-types.md` in full — it is your template.
2. **Read every source file** you are assigned. The source is ground truth. Verify every
   signature, field name, default value, enum member, and exception against it. NO drift,
   no invention. If a symbol's behaviour surprises you or looks like a bug/inconsistent
   API, document the ACTUAL behaviour and add a one-line note to `docs/BUILD-LOG.md`
   under a `## Doc-surfaced findings` heading (create it if absent) — do not smooth it over.

## Page structure (mirror the exemplar precisely)
- `# Title`
- A 1–2 sentence intro of what the cluster is.
- A bold `**Symbols on this page:**` line listing every assigned symbol as `` `code` `` separated by ` · `.
- `---`
- `## Core` — plain English, every term defined before use. ZERO unexplained jargon: no
  "taint", "fan-out", "fluid", "ADR", "idempotent" without a gloss in plain words.
- `## Ramps up` — mechanics, invariants, edge cases, design rationale. Cite the relevant
  ADR by number with a link to `../architecture/decisions/` when one applies. Use `###` subheads.
- `## API reference` — exact signature/fields/members for EVERY assigned symbol, verified
  against source. Use tables for model fields (Field | Type | Default | Notes) and enum
  members (Member | Value | Meaning). Use fenced `python` blocks for function signatures.
- `## Example` — at least one DETERMINISTIC runnable example, followed by a collapsible
  output block in this exact form:
  ```
  ??? success "▶ Output"

      ```text
      <exact output>
      ```
  ```

## Determinism (hard requirement — VETO if violated)
- Examples MUST be deterministic: pure functions/helpers, or `MockRuntime` / fixtures.
  NEVER a live model call, network, real clock, or random value in shown output.
- You MUST actually run your example with `uv run python - <<'PY' ... PY` and paste the
  REAL output byte-for-byte into the `▶ Output` block. Never invent output.
- If output contains a UUID/timestamp/path that varies, either avoid printing it or print
  a stable derived fact (e.g. `len(new_id())`), as the exemplar does.

## Style (Concision VETO)
- Match the exemplar's density. Cut filler: "basically", "in order to", "it's worth
  noting", "simply", "note that". No cross-tier duplication — say it once at the right tier.
- Imperative, precise, declarative. Prefer tables to prose for field/member enumerations.

## Output of your task
Write the page to its target path with the Write tool, then reply with: the path, the
count of symbols covered, and the verified output you pasted. Do not edit any other file
except appending to `docs/BUILD-LOG.md` if you surface a finding.
