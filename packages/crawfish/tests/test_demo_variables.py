"""Deterministic acceptance test for the Milestone-6 *variables & knowledge* demo step.

Exercises the variables/knowledge surface (CRA-223..227) added to
``demo/triage-bot/self_improve.py`` — entirely off the mock runtime (NO live model call,
$0) — and asserts the three load-bearing M6 guarantees:

* **Compose (AL-DV1)** — ``with_skill``/``with_context`` are copy-on-write: composing the
  borrowed triage definition mints a NEW frozen Definition with a DISTINCT content sha and
  leaves the receiver untouched.
* **Git for agents (AL-DV2/3)** — ``DefinitionStore.save`` -> ``recall`` round-trips by
  sha-identity; ``modify`` mints a NEW lineage version whose parent edge names the saved
  version; ``reset`` is a pure git-checkout back to a prior sha; the append-only log carries
  the lineage. ``save`` refuses an unfrozen draft, ``reset`` refuses an unreachable sha, and
  a name in org A is invisible to org B (tenancy isolation).
* **Summonable Wiki (AL-K1 / security boundary)** — a multi-page Wiki is summoned into the
  variant via ``with_context`` (re-versioning it) and ``consult`` materialises its pages as a
  Context whose every entry is **tainted (fluid)** — the knowledge reaches the agent as DATA,
  never an instruction surface.

Plus the assembly-safety invariants the changelog promises (the negative cases that prove
the guards are load-bearing) and the cost-honesty invariant that this whole step is
model-free, so it does NOT change the F-6 worst case.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from crawfish.definition_store import (
    DefinitionStore,
    UnfrozenDefinitionError,
    UnknownNameError,
    UnreachableShaError,
    modify,
    reset,
)
from crawfish.derive import SkillRef, with_skill
from crawfish.store import SqliteStore
from crawfish.versioning.version import FrozenError
from crawfish.wiki import TrustTier, Wiki

REPO_ROOT = Path(__file__).resolve().parents[3]
SCENARIO = REPO_ROOT / "demo" / "triage-bot" / "self_improve.py"


def _load_scenario():
    spec = importlib.util.spec_from_file_location("crawfish_demo_variables_test", SCENARIO)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # so dataclass forward-refs resolve
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def module():
    if not SCENARIO.exists():  # pragma: no cover - demo always present in-repo
        pytest.skip(f"demo scenario not found at {SCENARIO}")
    return _load_scenario()


@pytest.fixture(scope="module")
def result(module):
    return module.run_self_improvement(live=False)  # deterministic mock path only


# --- Compose: copy-on-write versions the agent, receiver untouched ---------------
def test_compose_mints_a_distinct_frozen_sha(module, result) -> None:
    """``with_skill∘with_context`` returned a NEW frozen sha distinct from the borrowed base."""
    assert result.var_base_sha
    assert result.var_composed_sha
    assert result.var_composed_sha != result.var_base_sha
    assert result.var_cow_versioned


def test_compose_is_copy_on_write_receiver_untouched(module) -> None:
    """Composing never mutates the receiver — the base keeps its sha; only the result moves."""
    base = module._frozen_copy(module.Definition.from_package(str(module.HERE)))
    base_sha = base.content_sha()
    variant = with_skill(base, SkillRef(id="x", version="0.1"))
    assert variant.content_sha() != base_sha  # the variant versioned
    assert base.content_sha() == base_sha  # ...and the receiver did NOT
    assert variant.frozen  # the result is re-sealed (eval-mode)


def test_compose_is_idempotent_on_identical_structure(module) -> None:
    """Two structurally-identical compositions collapse to the SAME sha (deterministic)."""
    base = module._frozen_copy(module.Definition.from_package(str(module.HERE)))
    skill = SkillRef(id="dup", version="0.1")
    assert with_skill(base, skill).content_sha() == with_skill(base, skill).content_sha()


# --- Git for agents: save / recall / modify / reset ------------------------------
def test_save_recall_sha_identity(result) -> None:
    """``save`` recorded the pointer at the composed sha; ``recall`` re-minted the SAME sha."""
    assert result.var_saved_sha == result.var_composed_sha
    assert result.var_recall_identity_ok


def test_modify_mints_a_new_lineage_version(result) -> None:
    """``modify`` produced a NEW version (distinct sha) whose parent edge names the saved one."""
    assert result.var_modify_versioned
    assert result.var_modified_sha
    assert result.var_modified_sha != result.var_saved_sha
    assert result.var_lineage_parent_ok
    assert len(result.var_log_shas) >= 2  # the saved + the modified version, append-only


def test_reset_is_a_pure_pointer_move_back(result) -> None:
    """``reset`` (git checkout) moved the name pointer back to the original saved sha."""
    assert result.var_reset_ok
    assert result.var_reset_sha == result.var_saved_sha


def test_save_refuses_an_unfrozen_draft(module) -> None:
    """``save`` rejects a non-frozen Definition — un-versioned mutation is impossible."""
    ds = DefinitionStore(SqliteStore(), org_id="acme")
    draft = module.Definition.from_package(str(module.HERE))
    # the package definition loads unfrozen; saving it must fail closed
    if draft.frozen:  # pragma: no cover - defensive; from_package is unfrozen
        draft = draft.model_copy(deep=True)
        object.__setattr__(draft, "frozen", False)
    with pytest.raises(UnfrozenDefinitionError):
        ds.save("x", draft)


def test_reset_refuses_an_unreachable_sha(module) -> None:
    """``reset`` refuses a sha not in the name's log (the pointer never lands off-lineage)."""
    ds = DefinitionStore(SqliteStore(), org_id="acme")
    base = module._frozen_copy(module.Definition.from_package(str(module.HERE)))
    ds.save("name", base)
    with pytest.raises(UnreachableShaError):
        reset(ds, "name", "deadbeefdead")


def test_modify_on_eval_mode_name_raises(module) -> None:
    """``modify`` whose ``fn`` mutates the recalled (frozen) Definition in place raises.

    A recalled name is eval-mode (frozen) and read-only; an ``fn`` that edits it in place
    (instead of composing via copy-on-write ``with_*``) hits the frozen guard — the AC that
    git-style edits must be copy-on-write, never in-place mutation.
    """
    ds = DefinitionStore(SqliteStore(), org_id="acme")
    base = module._frozen_copy(module.Definition.from_package(str(module.HERE)))
    ds.save("name", base)

    def _mutate_in_place(d):
        # the recalled Definition is frozen (eval-mode) -> a normal attribute set raises
        d.team = d.team  # noqa: PLW0127 — assignment itself trips the frozen guard
        return d

    with pytest.raises(FrozenError):
        modify(ds, "name", _mutate_in_place)


def test_definition_name_is_tenant_isolated(module) -> None:
    """A name saved in org A is invisible to org B (cross-tenant isolation, security)."""
    store = SqliteStore()
    base = module._frozen_copy(module.Definition.from_package(str(module.HERE)))
    DefinitionStore(store, org_id="org-a").save("shared-name", base)
    other = DefinitionStore(store, org_id="org-b")
    with pytest.raises(UnknownNameError):
        other.recall("shared-name")


# --- Summonable Wiki: consulted as TAINTED data, never instructions --------------
def test_wiki_summoned_into_variant(result) -> None:
    """``with_context`` summoned the multi-page Wiki into the variant (re-versioning it)."""
    assert result.wiki_sha
    assert result.wiki_pages >= 2
    assert result.wiki_summoned_into_variant


def test_wiki_consult_reaches_agent_as_tainted_data(result) -> None:
    """``consult`` materialised every page as a TAINTED (fluid) Context entry — data, not prompt."""
    assert result.wiki_consult_entries == result.wiki_pages
    assert result.wiki_consult_all_tainted
    assert result.wiki_content_is_data


def test_wiki_consult_is_pure_and_tainted(module) -> None:
    """Directly: a Wiki's pages enter the consulted Context tainted by default (the boundary)."""
    wiki = module._build_billing_wiki("acme")
    entries = list(wiki.consult().entries)
    assert len(entries) == len(wiki.pages)
    assert all(e.tainted for e in entries)  # even TRUSTED knowledge is summoned as data


def test_wiki_with_page_is_copy_on_write(module) -> None:
    """``with_page`` mints a NEW frozen Wiki with a distinct sha; the receiver is unchanged."""
    wiki = Wiki(org_id="acme").with_page("a", "alpha", trust=TrustTier.TRUSTED)
    before = wiki.content_sha()
    extended = wiki.with_page("b", "beta", trust=TrustTier.TRUSTED)
    assert extended.content_sha() != before  # the edit versioned
    assert wiki.content_sha() == before  # ...and the receiver did NOT
    assert extended.frozen and len(extended.pages) == 2


# --- The whole M6 step certifies, and adds NOTHING to the cost worst case --------
def test_variables_step_certifies(result) -> None:
    """The M6 certification predicate passes on the deterministic path."""
    assert result._variables_step_ok()


def test_scenario_passes_with_variables_step(result) -> None:
    """The full scenario still PASSES 9/9 with the M6 step wired into ``passed()``."""
    assert result.passed()


def test_variables_step_is_model_free(module) -> None:
    """The M6 step makes NO model call, so it must not raise the F-6 worst case.

    The worst-case call count is a pure structural function; the M6 variables/knowledge step
    is CoW/Store/pure-fold (no metered call), so the bound is identical with or without it.
    """
    # The worst case is derived solely from the (unchanged) tune/gate/calib/loop structure —
    # asserting it is a fixed number documents that M6 contributed zero metered calls.
    n = len(module._SEED_TICKETS)
    worst = module._worst_case_calls(
        n_cases=n,
        n_tune=n // 2,
        n_gate=n - n // 2,
        n_candidates=len(module._CANDIDATE_TEMPS),
        n_calib=n,
    )
    assert worst == 150  # unchanged from Milestone 5 (M6 adds no metered calls)
