"""CRA-197 / F-4 acceptance: GoldenSet.from_corrections + the `correction` kind.

Covers the five acceptance criteria against a real ``SqliteStore`` in ``tmp_path``:

1. count matches the ledger (N corrections -> N cases);
2. cross-org isolation (org "a" corrections invisible to org "b");
3. the ``correction`` kind is queryable/filterable from the store;
4. provenance/taint gate (Security S4): an untrusted/fluid correction is
   quarantined, never admitted to the GoldenSet as trusted ground truth;
5. the authored-GoldenSet fallback still works (construct + add directly).

Deterministic: no live model call, no wall-clock read (``ts`` is passed in).
"""

from __future__ import annotations

import shutil
from pathlib import Path

from crawfish.emission import (
    CorrectionType,
    EmissionKind,
    Provenance,
    emit_correction,
    read_corrections,
)
from crawfish.eval import EvalCase, GoldenSet
from crawfish.store import SqliteStore


def _store(tmp_path: Path) -> SqliteStore:
    return SqliteStore(tmp_path / "corrections.db")


def _emit(
    store: SqliteStore,
    *,
    run_id: str,
    ctype: CorrectionType,
    provenance: Provenance = Provenance.TRUSTED,
    tainted: bool = False,
    org_id: str = "local",
    expected: object = "fixed",
    produced: object = "wrong",
) -> str:
    em = emit_correction(
        store,
        run_id=run_id,
        correction_type=ctype,
        provenance=provenance,
        tainted=tainted,
        org_id=org_id,
        inputs={"prompt": "do the thing"},
        expected=expected,
        produced=produced,
        ts=1.0,
    )
    return em.id


# -- (1) count matches the ledger --------------------------------------------
def test_count_matches_ledger(tmp_path: Path) -> None:
    store = _store(tmp_path)
    types = [
        CorrectionType.HUMAN_REVERT,
        CorrectionType.CI_FAILURE,
        CorrectionType.REVIEW_REJECT,
        CorrectionType.HUMAN_REVERT,
    ]
    for i, ct in enumerate(types):
        _emit(store, run_id=f"r{i}", ctype=ct)

    gs = GoldenSet.from_corrections(store)
    assert len(gs.cases()) == len(types)
    # the curated cases carry the corrected output as the label
    assert all(c.label == "fixed" for c in gs.cases())
    store.close()


# -- (2) cross-org isolation --------------------------------------------------
def test_cross_org_isolation(tmp_path: Path) -> None:
    store = _store(tmp_path)
    for i in range(3):
        _emit(store, run_id=f"a{i}", ctype=CorrectionType.CI_FAILURE, org_id="a")
    _emit(store, run_id="b0", ctype=CorrectionType.CI_FAILURE, org_id="b")

    gs_a = GoldenSet.from_corrections(store, org_id="a")
    gs_b = GoldenSet.from_corrections(store, org_id="b")
    assert len(gs_a.cases()) == 3
    assert len(gs_b.cases()) == 1
    # org "b" cannot see org "a" corrections at the corpus level either
    assert len(read_corrections(store, org_id="b")) == 1
    store.close()


# -- (3) the correction kind is queryable / filterable -----------------------
def test_correction_kind_is_queryable(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _emit(store, run_id="r0", ctype=CorrectionType.HUMAN_REVERT)
    _emit(store, run_id="r1", ctype=CorrectionType.CI_FAILURE)
    _emit(store, run_id="r2", ctype=CorrectionType.CI_FAILURE)

    all_c = read_corrections(store)
    assert len(all_c) == 3
    assert all(em.kind is EmissionKind.CORRECTION for em in all_c)

    only_ci = read_corrections(store, kinds=(CorrectionType.CI_FAILURE,))
    assert len(only_ci) == 2
    assert {em.attrs["correction_type"] for em in only_ci} == {"ci_failure"}

    # filtering through from_corrections narrows the built set too
    gs = GoldenSet.from_corrections(store, kinds=(CorrectionType.HUMAN_REVERT,))
    assert len(gs.cases()) == 1
    store.close()


# -- (4) provenance / taint gate (Security S4) -------------------------------
def test_untrusted_correction_is_quarantined(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _emit(store, run_id="trusted", ctype=CorrectionType.REVIEW_REJECT)
    # an UNTRUSTED correction (authored from fluid session data)
    _emit(
        store,
        run_id="untrusted",
        ctype=CorrectionType.REVIEW_REJECT,
        provenance=Provenance.UNTRUSTED,
    )
    # a TRUSTED-labelled but fluid-TAINTED correction must ALSO be quarantined
    _emit(
        store,
        run_id="tainted",
        ctype=CorrectionType.REVIEW_REJECT,
        provenance=Provenance.TRUSTED,
        tainted=True,
    )

    # the corpus records all three (audit), but only the clean trusted one is admitted
    assert len(read_corrections(store)) == 3
    gs = GoldenSet.from_corrections(store)
    cases = gs.cases()
    assert len(cases) == 1
    assert cases[0].metadata["provenance"] == Provenance.TRUSTED.value
    assert cases[0].metadata["run_id"] == "trusted"
    store.close()


def test_tainted_trusted_is_not_admitted(tmp_path: Path) -> None:
    # A correction can carry a value derived from fluid input even while labelled
    # TRUSTED; the AND-gate must still quarantine it (taint wins).
    store = _store(tmp_path)
    _emit(
        store,
        run_id="r0",
        ctype=CorrectionType.HUMAN_REVERT,
        provenance=Provenance.TRUSTED,
        tainted=True,
    )
    assert len(GoldenSet.from_corrections(store).cases()) == 0
    store.close()


# -- (5) authored-GoldenSet fallback still works -----------------------------
def test_authored_goldenset_fallback(tmp_path: Path) -> None:
    store = _store(tmp_path)
    gs = GoldenSet(store, "authored")
    gs.add(EvalCase(inputs={"q": "1+1"}, output="3", label="2"))
    gs.add(EvalCase(inputs={"q": "2+2"}, output="5", label="4"))
    cases = gs.cases()
    assert len(cases) == 2
    assert {c.label for c in cases} == {"2", "4"}
    store.close()


# -- determinism: same ledger -> same built set ------------------------------
def test_deterministic_given_fixed_ledger(tmp_path: Path) -> None:
    src = tmp_path / "src.db"
    store = SqliteStore(src)
    ids = [_emit(store, run_id=f"r{i}", ctype=CorrectionType.CI_FAILURE) for i in range(3)]
    store.close()

    def build() -> list[str]:
        copy = tmp_path / "copy.db"
        shutil.copyfile(src, copy)
        s = SqliteStore(copy)
        case_ids = sorted(c.id for c in GoldenSet.from_corrections(s).cases())
        s.close()
        copy.unlink()
        return case_ids

    assert build() == build() == sorted(ids)
