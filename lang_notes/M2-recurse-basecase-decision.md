# M2 — `recurse` base_case must receive authoritative depth  (found by verifier-m2 live gate)

### D-M2-fix — Pass engine-authoritative depth into `BaseCase`  (CRA-208, arch+security)
**Defect (caught live, reproduces bit-identically):** the demo's multi-part recursion inferred
descent depth from a `_recurse_depth` marker that the MOCK body emits but the REAL model has no
obligation to echo. Live ⇒ depth never climbs ⇒ base_case never fires ⇒ descends to max_depth,
folds 0 parts. The deterministic test only ran the mock body, masking it.

**Root cause (framework):** `BaseCase = Callable[[Output[JSONValue]], bool]` — the base-case
predicate only sees the (stochastic) model Output, never the engine's authoritative descent depth.
Any predicate that needs to know "how deep am I" is forced to read it from model output, which is
unsound: **a termination/bound decision must not depend on stochastic output.**

**Fork:**
- A: change `BaseCase` to `Callable[[Output, int], bool]` — the engine passes the authoritative
  `depth` (it already owns `depth`/`depth_reached`). Predicates may use content, depth, or both.
- B: leave the signature; make the demo fold exactly `max_depth` real children and drop base_case.
- C: leave it; demo parses the real prose to detect completion.

**Decision:** A. The engine owns depth; handing it to the predicate is strictly more expressive
and removes the unsound model-echo dependency. `recurse()` shipped on this same (unmerged) branch,
so there are no external consumers — the signature change is free now and never again.
**Rejected:** B abandons the content-based base_case the issue specifies; C re-introduces a
stochastic dependency in a control-flow decision (same class of bug).
**Spine impact:** security/versioning — termination bounds are now decided from trusted engine
state, not fluid model output. Aligns with "FLUID never drives control-flow / static slots."

**Two-owner fix (sequential — framework first):**
1. `impl-program` (workflow.py): `BaseCase = Callable[[Output[JSONValue], int], bool]`; call site
   `self.base_case(current, depth)`; update docstring + CRA-208 changelog + `test_recurse.py`
   base_case callables to take depth.
2. `demo-runner` (self_improve.py): base_case uses the passed depth (stop when `depth + 1 >= parts`);
   FOLD the real descent-order child Outputs (prose) instead of requiring a structured marker; ADD a
   composition test with a MARKER-LESS body proving recurse stops on base_case and folds N parts live.

**Test-gap closed:** the composition suite must exercise a body whose Output omits demo markers
(simulating the real model), so this class of defect can never again reach the live gate green.
