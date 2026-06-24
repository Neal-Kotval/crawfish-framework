# Milestone 4 — Taming stochasticity (CRA-215/216/217/218)

Branch `cra/m-4` off `cra/m-3`. Thesis advance: bound the stochastic primitive — vote it down
(Quorum), let it decline (abstention), distil its learned invariants into pure guards
(house-guard), and constrain its surface (grammar decoding).

## File ownership (one owner per file) — single parallel wave (all disjoint)
- **impl-quorum** → NEW `runtime/quorum.py` + `nodes/aggregator.py` : **215 TS-1** typed Quorum /
  self-consistency — `QuorumRuntime` samples the same request k times (each charges the budget),
  aggregates by a typed vote; ties/no-majority resolve to a declared default (Router parity).
- **impl-abstain** → NEW `abstain.py` + `escalate.py` : **216 TS-4** abstention as a typed Output
  discipline — `Abstention` payload (reason, confidence, carries taint) + `abstain_below(threshold)`;
  uses the M3 `abstention_threshold`/`extract_confidence` (confidence MEASURED, never trusted).
- **impl-guard** → NEW `guard.py` : **217 TS-7/R4** house-guard (learned-then-distilled) — model
  PROPOSES a rule (the one stochastic leaf), distil to a PURE deterministic predicate, earn
  enforcement; mines `from_corrections` (F-4, eval.py — import only). The deepest thesis expression.
- **impl-decode** → `runtime/base.py` (RunRequest `grammar`/`decode_seed`, per-call, out of content
  hash) + `run.py` (plumb) + NEW `grammar.py` : **218 TS-8** constrained/grammar-guided decoding as
  a runtime-mediated capability.

## Waves
- **Wave 1 (all parallel, disjoint files):** impl-quorum ∥ impl-abstain ∥ impl-guard ∥ impl-decode.
- No M4 inter-deps (216 uses shipped M3 calibrate; 217 uses shipped F-4 from_corrections).
- Release-manager owns `__init__.py` exports; impl-quorum must NOT edit runtime/base.py (impl-decode owns it).

## Live evidence target (RUNBOOK)
Ambiguous tickets go to a Quorum vote; low-confidence ones ABSTAIN (typed Output); a house-guard
blocks a disallowed output; a structured field is produced under constrained decoding. Budget
respected; bit-identical replay.
