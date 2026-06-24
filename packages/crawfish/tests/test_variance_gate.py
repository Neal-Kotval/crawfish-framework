"""AL-T5 / CRA-212 — variance-aware promotion gate.

Acceptance (issue §Acceptance):

* ``std=0`` ⇒ byte-identical to today (the single-point gate).
* Baseline 0.80±0.05, candidate 0.82 (in band) ⇒ rejected; candidate 0.92
  (beyond ``k·std``) ⇒ accepted.
* A candidate that maxes one metric but regresses another is still rejected
  (the F-3 hard gate is unchanged).
* Per-metric std persists with ``org_id`` and reloads across a fresh handle.
* Winner's-curse correction shrinks the stored baseline.
* Same recorded scores + std ⇒ identical decision (deterministic).
"""

from __future__ import annotations

import pytest

from crawfish.eval import (
    PromotionVerdict,
    gate_against_baseline,
    load_baseline,
    load_baseline_std,
    promote_against_baseline,
    save_baseline,
)
from crawfish.metrics import is_regression
from crawfish.store import SqliteStore


@pytest.fixture
def store(tmp_path) -> SqliteStore:
    return SqliteStore(tmp_path / "gate.db")


# -- std persistence ---------------------------------------------------------
def test_baseline_std_persists_and_reloads(tmp_path) -> None:
    db = tmp_path / "persist.db"
    store = SqliteStore(db)
    save_baseline(store, "triage", {"acc": 0.80}, std={"acc": 0.05}, org_id="acme")
    store.close()

    reopened = SqliteStore(db)
    assert load_baseline(reopened, "triage", org_id="acme") == {"acc": 0.80}
    assert load_baseline_std(reopened, "triage", org_id="acme") == {"acc": 0.05}
    # Org isolation: the band does not leak to another tenant.
    assert load_baseline_std(reopened, "triage", org_id="other") is None


def test_save_baseline_without_std_leaves_no_std_record(store: SqliteStore) -> None:
    # Back-compat: a baseline saved the old way has no std record at all.
    save_baseline(store, "triage", {"acc": 0.80})
    assert load_baseline_std(store, "triage") is None


# -- the in-band vs beyond-band decision (the worked example) ----------------
def test_in_band_candidate_rejected(store: SqliteStore) -> None:
    # Baseline 0.80 ± 0.05. The candidate at 0.82 is a +0.02 gain — inside the
    # k·std noise band — so it must NOT promote.
    save_baseline(store, "triage", {"acc": 0.80}, std={"acc": 0.05})
    verdict = promote_against_baseline(store, "triage", {"acc": 0.82}, primary="acc")
    assert isinstance(verdict, PromotionVerdict)
    assert verdict.promoted is False
    assert verdict.cleared_band is False
    assert verdict.regressed is False
    # The stored baseline is unchanged (a rejected candidate writes nothing).
    assert load_baseline(store, "triage") == {"acc": 0.80}


def test_beyond_band_candidate_promoted(store: SqliteStore) -> None:
    # The candidate at 0.92 is a +0.12 gain — well beyond k·0.05 — so it promotes.
    save_baseline(store, "triage", {"acc": 0.80}, std={"acc": 0.05})
    verdict = promote_against_baseline(store, "triage", {"acc": 0.92}, primary="acc")
    assert verdict.promoted is True
    assert verdict.cleared_band is True
    # The baseline advances to the promoted score (no fresh sample ⇒ no shrink).
    assert load_baseline(store, "triage") == {"acc": 0.92}
    # The std band is carried forward so the next round stays variance-aware.
    assert load_baseline_std(store, "triage") == {"acc": 0.05}


# -- the F-3 hard gate is unchanged: a regression on ANY metric vetoes -------
def test_maxing_one_metric_while_regressing_another_is_rejected(store: SqliteStore) -> None:
    save_baseline(
        store,
        "triage",
        {"acc": 0.80, "format": 0.90},
        std={"acc": 0.05, "format": 0.01},
    )
    # acc jumps to 1.0 (clears its band) but format drops 0.90 -> 0.50, far past
    # its tiny 0.01 band ⇒ the hard gate vetoes regardless of the acc gain.
    verdict = promote_against_baseline(store, "triage", {"acc": 1.0, "format": 0.50}, primary="acc")
    assert verdict.promoted is False
    assert verdict.regressed is True
    assert load_baseline(store, "triage") == {"acc": 0.80, "format": 0.90}


def test_within_noise_dip_on_secondary_does_not_veto(store: SqliteStore) -> None:
    # format dips 0.90 -> 0.89 (inside its 0.05 band) — tolerated; acc clears.
    save_baseline(
        store,
        "triage",
        {"acc": 0.80, "format": 0.90},
        std={"acc": 0.02, "format": 0.05},
    )
    verdict = promote_against_baseline(
        store, "triage", {"acc": 0.95, "format": 0.89}, primary="acc"
    )
    assert verdict.regressed is False
    assert verdict.promoted is True


# -- back-compat: std=0 (or no std record) reduces to today's gate -----------
def test_std_zero_reduces_to_single_point_gate(store: SqliteStore) -> None:
    baseline = {"acc": 0.80}
    save_baseline(store, "triage", baseline, std={"acc": 0.0})
    # With a zero band, ANY positive gain promotes and ANY drop regresses —
    # exactly is_regression's behaviour.
    promoted = promote_against_baseline(store, "triage", {"acc": 0.81}, primary="acc")
    assert promoted.promoted is True
    assert promoted.primary_band == 0.0

    save_baseline(store, "triage", baseline, std={"acc": 0.0})
    dropped = promote_against_baseline(store, "triage", {"acc": 0.79}, primary="acc")
    assert dropped.promoted is False
    assert dropped.regressed is True
    # Equivalent to the legacy single-point verdict.
    assert dropped.regressed is is_regression(baseline, {"acc": 0.79})


def test_no_std_record_behaves_like_std_zero(store: SqliteStore) -> None:
    save_baseline(store, "triage", {"acc": 0.80})  # no std record at all
    assert promote_against_baseline(store, "triage", {"acc": 0.805}, primary="acc").promoted
    assert load_baseline_std(store, "triage") is None


def test_no_baseline_seeds_and_promotes(store: SqliteStore) -> None:
    verdict = promote_against_baseline(store, "triage", {"acc": 0.5}, primary="acc")
    assert verdict.promoted is True
    # Mirrors gate_against_baseline's "no baseline ⇒ pass".
    assert gate_against_baseline(store, "fresh", {"acc": 0.5}) is True


# -- winner's-curse correction (F-8) -----------------------------------------
def test_winners_curse_shrinks_stored_baseline(store: SqliteStore) -> None:
    save_baseline(store, "triage", {"acc": 0.80}, std={"acc": 0.02})
    # Selected (optimistic) acc 0.95 promotes; the fresh independent sample reads
    # 0.90, so the stored baseline is shrunk toward the unbiased estimate.
    verdict = promote_against_baseline(
        store,
        "triage",
        {"acc": 0.95},
        primary="acc",
        fresh_sample={"acc": 0.90},
        shrink_weight=1.0,
    )
    assert verdict.promoted is True
    # weight=1.0 ⇒ stored baseline is exactly the fresh estimate, never the inflated max.
    assert load_baseline(store, "triage") == {"acc": 0.90}


def test_partial_shrink_interpolates(store: SqliteStore) -> None:
    save_baseline(store, "triage", {"acc": 0.80}, std={"acc": 0.02})
    promote_against_baseline(
        store,
        "triage",
        {"acc": 1.00},
        primary="acc",
        fresh_sample={"acc": 0.80},
        shrink_weight=0.5,
    )
    # (1-0.5)*1.00 + 0.5*0.80 = 0.90
    assert load_baseline(store, "triage") == {"acc": 0.90}


# -- determinism -------------------------------------------------------------
def test_decision_is_deterministic(tmp_path) -> None:
    decisions = []
    for i in range(3):
        store = SqliteStore(tmp_path / f"det{i}.db")
        save_baseline(store, "triage", {"acc": 0.80, "f1": 0.70}, std={"acc": 0.05, "f1": 0.03})
        v = promote_against_baseline(store, "triage", {"acc": 0.91, "f1": 0.71}, primary="acc")
        decisions.append((v.promoted, v.regressed, v.cleared_band, v.primary_band))
    assert len(set(decisions)) == 1


def test_missing_primary_raises(store: SqliteStore) -> None:
    save_baseline(store, "triage", {"acc": 0.80}, std={"acc": 0.05})
    with pytest.raises(KeyError):
        promote_against_baseline(store, "triage", {"acc": 0.95}, primary="missing")
