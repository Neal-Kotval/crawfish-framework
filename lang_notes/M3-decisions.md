# Milestone 3 — Tunable ML library: decision log

## CRA-209 (AL-T1 two-axis mode) + CRA-213 (AL-T3 Objective) — impl-tuner forks

### D-M3-1 — train()/eval() as PyTorch-mirroring module functions; `eval` shadows builtin
**Decision:** ship `train(defn)->Definition` (unfrozen, fresh Version) and `eval(defn)->Definition`
(re-freeze to content_sha; idempotent) mirroring `torch`'s `.train()/.eval()`. Exported at package
root (`cw.train`/`cw.eval`). `eval` deliberately shadows the builtin (`# noqa: A001`).
**Rationale:** the thesis vocabulary IS train/eval; `import crawfish as cw` usage makes the shadow a
non-issue. **Caveat:** `from crawfish import *` would shadow builtin `eval` — the package is used as
`cw.`, not star-import. **Spine impact:** versioning — train = mutable/CoW, eval = frozen; only eval
mode may fire consequential Sinks (enforced by `guard_consequential`).

### D-M3-2 — `Definition.tune` wiring delegated to the definition/compiler owner (hash-neutral)
**Fork:** CRA-209's "tune round-trips through export() and changes the sha" needs `Definition.tune`
folded into the content hash, but `definition/types.py`+`compiler.py` are not the tuner's files.
**Decision:** the tuner ships `TuneSpec`/`KnobDomain`/`tune_spec_sha` + train/eval/guard via the
existing freeze/CoW; a focused `impl-defn-tune` wires `Definition.tune` into `content_sha`/`export`
**hash-neutral when empty** (tune-less artifacts keep their sha — the demo lock value
`0.1-7113bfa78543` MUST be unchanged). A non-empty tune changes the sha (tuning versions the agent).
**Spine impact:** versioning/migration — empty-tune is hash-neutral (no migration); non-empty tune is
a real content change. CONTENT_HASH_VERSION stays back-compatible for tune-less Definitions.

### D-M3-3 — `guard_consequential(defn)` is the eval-only-Sink gate, not a sink.py edit
**Fork:** the spine says "consequential Sinks fire only in eval/frozen mode," but `nodes/sink.py`
isn't the tuner's file and `Sink` doesn't take a Definition today.
**Decision:** ship `guard_consequential(defn)` (raises `FrozenError` unless eval-mode) as the callable
the consequential boundary invokes; the demo already gates frozen-before-sink. Wiring it into the
sink/run egress site is tracked for Milestone S hardening (one-owner-per-file: sink.py owner does it).
**Spine impact:** none yet (guard exists; universal enforcement is the M-S follow-up).

### D-M3-4 — Objective decoupled from calibrate (ece is a passed-in value)
**Decision:** `Objective.value(scores, *, cost_usd, ece) = Σwᵢ·scoreᵢ − λ·cost − μ·ece` takes `ece`
and `cost_usd` as VALUES (cost from the deterministic `estimate_cost`), so it does not import the
calibrate implementation — letting 209/213 (tuner) and 211 (calibrate) build in parallel.
**Spine impact:** cost — the objective regularizes on real cost + calibration error, advancing the
cost-honesty + calibration thesis without a build-order coupling.
