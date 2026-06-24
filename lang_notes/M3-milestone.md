# Milestone 3 — Tunable ML library (CRA-209..214)  [FLAGSHIP slice]

Branch `cra/m-3` off `cra/m-2`. Thesis advance: the **PyTorch-for-LLMs half** — `mutable` =
train/eval mode; per-knob `tunable`; calibrate variance; promote under a variance-aware gate; a
cost-regularized objective; state_dict transfer. This is the flagship vertical that proves the
train/eval thesis end to end.

## File ownership (one owner per file)
- **impl-tuner** → `tuner.py`, `borrow.py`, `cost.py` : **209 AL-T1** (two-axis mode unifier:
  `TuneSpec`/`tune.toml` content-hashed into `Definition.tune`; per-knob `tunable` + `train()`/`eval()`)
  **+ 213 AL-T3** (cost-regularized `Objective.value = Σwᵢ·scoreᵢ − λ·cost − μ·ece`; ece passed in as a
  value, so decoupled from calibrate's impl).
- **impl-calibrate** → `metrics.py`, `escalate.py` : **211 AL-T4/TS-2** `cw.calibrate(definition, golden,
  *, runs, ctx, runtime) -> CalibrationReport` (per-metric std, ECE, abstention threshold; N runs under
  distinct derived seeds). FLAGSHIP primitive; consumed by 212/213.
- **impl-learning** → `learning.py` : **210 AL-T2** `state_dict()/load_state()` (tunable knobs only:
  prompt/few-shots/model/temperature/sample_k/context_strategy/policies, references-by-version)
  **+ 214 AL-T6** explore-rate `ServingLoop` (route (1-ε) to promoted best, ε to a trial by seeded hash
  of item_id so replay re-explores deterministically).
- **impl-gate** → `eval.py` : **212 AL-T5** variance-aware promotion gate (store per-metric `*_std`
  alongside baseline; promote only when the gain clears noise) — imports 211's std + 209's mode.

## Waves
- **Wave 1 (parallel, disjoint):** impl-tuner (209+213) ∥ impl-calibrate (211).
- **Wave 2 (parallel, disjoint, after W1):** impl-learning (210+214) ∥ impl-gate (212).
  - 210 needs 209's knob/StateDict structure; 212 needs 211's std + 209's mode; 214 uses the promoted
    best. All W1 outputs are landed before W2 starts.

## Live evidence target (RUNBOOK / flagship demo)
`train()` the triage agent → show a calibration report (variance/ECE/abstention) → tune under the
cost-regularized objective → promote via the variance-aware gate → `state_dict` save/load the result;
prove deterministic replay + the gate's promote/reject under real variance.
