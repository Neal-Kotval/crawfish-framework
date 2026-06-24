"""The typed emission substrate — one signal everything emits.

Telemetry today is loose, untyped dicts written via ``Store.append_event`` and read
back via ``Store.events``. This module freezes the **contract** for a single typed
signal — :class:`Emission` — that every producer (runtime, tools, sinks, the tuner,
learning agents, the broker, the jail, observers, metrics) emits onto the append-only
ledger, and that every consumer (the dashboard #11, anomaly engine #14, inspector)
reads.

CRA-184 lands the *contract only*: the frozen model, the **closed**
:class:`EmissionKind` taxonomy, the required-``attrs`` schema per kind, and a
``schema_version`` so the ledger survives future kind/attr evolution. The behavioural
halves — routing the existing ``AgentRuntime._emit_telemetry`` through ``Emission``,
the ledger serialization, and the legacy-dict back-compat shim — land in CRA-171 and
the Store-migration work CRA-191; their entry points are stubs here.

Security spine: ``Emission.tainted`` propagates the fluid/untrusted marker across the
emission boundary. An emission carrying values derived from fluid input stays tainted,
so the dashboard and anomaly rules never treat untrusted content as trusted.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import Enum
from types import MappingProxyType
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from crawfish.core.ids import new_id
from crawfish.core.types import JSONValue

if TYPE_CHECKING:
    from crawfish.store.base import Store

__all__ = [
    "EMISSION_SCHEMA_VERSION",
    "EmissionKind",
    "REQUIRED_ATTRS",
    "Emission",
    "CorrectionType",
    "Provenance",
    "CORRECTION_RECORD_KIND",
    "emit",
    "read_emissions",
    "emit_correction",
    "read_corrections",
]

# Bump when the Emission envelope or any kind's required-attrs change. The Store
# migration (CRA-191) keys off this; the dashboard/inspector read it to stay
# forward/backward compatible.
EMISSION_SCHEMA_VERSION = 1


class EmissionKind(str, Enum):
    """The **closed** taxonomy of signals. Adding a kind is a contract change
    (bump :data:`EMISSION_SCHEMA_VERSION` and extend :data:`REQUIRED_ATTRS`)."""

    RUN_START = "run_start"  # a pipeline/agent run began
    RUN_FINISH = "run_finish"  # a run completed (terminal)
    MODEL = "model"  # one model turn (cost/tokens/model id)
    TOOL = "tool"  # a tool/MCP call (result is untrusted -> tainted)
    SINK = "sink"  # a consequential side effect was attempted/committed
    COMPACTION = "compaction"  # context was compacted/summarized
    OBSERVER = "observer"  # an ObserverEvent crossed into the stream
    METRIC = "metric"  # a measured Metric/Rubric value
    SECRET_LEASE = "secret_lease"  # the broker leased a secret to a node (#8)
    JAIL_VIOLATION = "jail_violation"  # the sandbox blocked an escape attempt (#9)
    CORRECTION = "correction"  # a ground-truth correction signal (F-4): feeds GoldenSets/guards


class CorrectionType(str, Enum):
    """The sub-category carried on a ``CORRECTION`` emission/record (F-4).

    These are the three correction *types* sourced by
    :meth:`crawfish.eval.GoldenSet.from_corrections` — each names *how* a wrong
    output was discovered, so a GoldenSet can be filtered to one signal source.
    """

    HUMAN_REVERT = "human_revert"  # a human reverted/undid an agent's output
    CI_FAILURE = "ci_failure"  # a CI/test gate failed on an agent's output
    REVIEW_REJECT = "review_reject"  # a reviewer rejected the output


class Provenance(str, Enum):
    """Who authored a ``CORRECTION`` — the trust class of its source (Security S4).

    Corrections feed guards/verifiers as ground truth, so a poisoned correction
    corpus is an attack surface (corpus poisoning). Provenance is the explicit
    control on WHO may emit a correction that enters a GoldenSet:

    * :attr:`TRUSTED` — authored by a trusted operator/CI/reviewer (static config,
      not session data). Admitted to a GoldenSet as ground truth.
    * :attr:`UNTRUSTED` — derived from fluid/untrusted session data. Quarantined:
      recorded for audit but **never** admitted as trusted ground truth.

    The taint gate (see :meth:`crawfish.eval.GoldenSet.from_corrections`) requires
    BOTH ``provenance == TRUSTED`` AND ``tainted is False``: a correction carrying
    a fluid-derived value is quarantined even if labelled trusted, so tainted input
    can never silently become a guard's ground truth.
    """

    TRUSTED = "trusted"
    UNTRUSTED = "untrusted"


# The Store record namespace for corrections. A correction is written BOTH as a
# ledger emission (the queryable taxonomy half) AND as a generic Store record under
# this kind, so the corpus is listable/filterable across runs and org-isolated via
# the existing record API (the F-2 pattern: no Store-file edits). The record id is
# the emission id; every row carries org_id.
CORRECTION_RECORD_KIND = "correction"


# Required attribute keys per kind. The values carried in ``Emission.attrs`` must
# include at least these keys; consumers may rely on their presence. This is the
# canonical schema referenced by CRA-171/#8/#9/#11 and the taxonomy doc
# (docs/architecture/emission-taxonomy.md). Frozen to prevent accidental drift.
REQUIRED_ATTRS: Mapping[EmissionKind, tuple[str, ...]] = MappingProxyType(
    {
        EmissionKind.RUN_START: ("runtime",),
        EmissionKind.RUN_FINISH: ("status",),
        EmissionKind.MODEL: ("model", "cost_usd"),
        EmissionKind.TOOL: ("tool",),
        EmissionKind.SINK: ("target", "committed"),
        EmissionKind.COMPACTION: ("strategy",),
        EmissionKind.OBSERVER: ("kind", "severity"),
        EmissionKind.METRIC: ("metric", "value"),
        EmissionKind.SECRET_LEASE: ("ref", "node_id"),
        EmissionKind.JAIL_VIOLATION: ("attempt", "severity"),
        # provenance is the WHO-may-emit control (Security S4); correction_type is
        # the human_revert/ci_failure/review_reject sub-category.
        EmissionKind.CORRECTION: ("correction_type", "provenance"),
    }
)


class Emission(BaseModel):
    """One typed signal on the append-only ledger. Frozen once created.

    ``attrs`` carries the kind-specific payload (see :data:`REQUIRED_ATTRS`).
    ``tainted`` propagates the fluid/untrusted marker across the emission boundary.
    """

    id: str = Field(default_factory=new_id)
    schema_version: int = EMISSION_SCHEMA_VERSION
    kind: EmissionKind
    run_id: str
    org_id: str = "local"  # tenancy key (CLAUDE.md: every Store row carries org_id)
    pipeline: str | None = None
    node_id: str | None = None  # agent/node that emitted, when applicable
    ts: float = 0.0  # epoch seconds; emitters stamp it, tests pass it for determinism
    attrs: dict[str, JSONValue] = Field(default_factory=dict)
    # Security: True when any value in ``attrs`` derives from fluid (untrusted) input.
    tainted: bool = False

    model_config = {"frozen": True}

    def missing_attrs(self) -> tuple[str, ...]:
        """Required-attr keys for this kind that are absent from ``attrs``.

        A pure contract check (no I/O): empty tuple means the emission satisfies
        its kind's schema. Used by CRA-171's emit path and the conformance suite.
        """
        required = REQUIRED_ATTRS.get(self.kind, ())
        return tuple(key for key in required if key not in self.attrs)

    def is_valid(self) -> bool:
        """True if ``attrs`` carries every key required for this kind."""
        return not self.missing_attrs()

    def to_event(self) -> dict[str, JSONValue]:
        """Serialize to a ledger event dict written via ``Store.append_event``.

        Uses ``model_dump(mode="json")`` so the dict is JSON-safe; ``kind`` is the
        enum's value string. The presence of the ``schema_version`` key (plus a
        ``kind`` that names a known :class:`EmissionKind`) is how :meth:`from_event`
        distinguishes a typed emission from a legacy loose dict.
        """
        data = self.model_dump(mode="json")
        # model_dump already serializes the Enum to its value via mode="json", but be
        # explicit so the contract is unambiguous regardless of pydantic settings.
        data["kind"] = self.kind.value
        return data

    @classmethod
    def from_event(cls, event: Mapping[str, JSONValue]) -> Emission:
        """Rehydrate from a (possibly legacy) ledger event dict.

        Lifts BOTH (a) typed emission dicts produced by :meth:`to_event` and
        (b) legacy loose event dicts (the ad-hoc telemetry written before the typed
        substrate landed) into a valid :class:`Emission`. Applies ``schema_version``
        defaulting (a missing version migrates to the current one). Tolerant by
        design: an unrecognized legacy dict still lifts into *some* emission (a
        ``METRIC`` carrying the raw payload under ``attrs``) rather than raising — old
        runs must remain inspectable.
        """
        data = dict(event)

        # -- (a) a typed emission produced by to_event -------------------------
        # The typed envelope is recognized by a known ``kind`` together with an
        # ``attrs`` mapping (the contract's payload slot). ``schema_version`` may be
        # absent on an older typed row — it is defaulted in ``_lift_typed`` — but a
        # legacy ObserverEvent dump also carries ``kind``, so the ``attrs`` mapping is
        # what disambiguates the typed envelope from a loose legacy dict.
        kind_raw = data.get("kind")
        if _is_known_kind(kind_raw) and isinstance(data.get("attrs"), dict):
            return cls._lift_typed(data)

        # -- (b) a legacy loose event dict -------------------------------------
        return cls._lift_legacy(data)

    # -- back-compat lifting helpers ---------------------------------------
    @classmethod
    def _lift_typed(cls, data: dict[str, JSONValue]) -> Emission:
        """Rehydrate a dict that already carries the typed envelope shape."""
        # schema_version migration: a future/lower version still lifts; we only
        # default a missing one. Real field migrations land per-version in CRA-191.
        # TODO(CRA-191): rotation/retention + per-version attr migrations.
        if data.get("schema_version") is None:
            data["schema_version"] = EMISSION_SCHEMA_VERSION
        return cls.model_validate(data)

    @classmethod
    def _lift_legacy(cls, data: dict[str, JSONValue]) -> Emission:
        """Map a known (or unknown) legacy loose dict to a typed Emission."""
        run_id = str(data.get("run_id") or data.get("trace_id") or "unknown")
        org_id = str(data.get("org_id") or "local")
        tainted = bool(data.get("tainted", False))
        kind, attrs, node_id = _classify_legacy(data)
        return cls(
            schema_version=EMISSION_SCHEMA_VERSION,
            kind=kind,
            run_id=run_id,
            org_id=org_id,
            pipeline=_opt_str(data.get("pipeline")),
            node_id=node_id,
            ts=_opt_float(data.get("ts")),
            attrs=attrs,
            tainted=tainted,
        )


def _is_known_kind(value: JSONValue) -> bool:
    if isinstance(value, EmissionKind):
        return True
    if not isinstance(value, str):
        return False
    return value in EmissionKind._value2member_map_


def _opt_str(value: JSONValue) -> str | None:
    return value if isinstance(value, str) else None


def _opt_float(value: JSONValue) -> float:
    return float(value) if isinstance(value, (int, float)) else 0.0


def _classify_legacy(
    data: Mapping[str, JSONValue],
) -> tuple[EmissionKind, dict[str, JSONValue], str | None]:
    """Map a legacy loose event dict to (kind, attrs, node_id).

    Recognizes the ad-hoc shapes the codebase wrote before the typed substrate:
    ``runtime.run`` telemetry, ``span`` run-lifecycle events, ``sink.write``
    records, ``context.compaction`` events, and ``ObserverEvent`` dumps. An
    unrecognized dict falls through to a generic ``METRIC`` carrying the raw
    payload so it still lifts and stays inspectable.
    """
    node_id = _opt_str(data.get("node_id"))

    # runtime.run telemetry -> MODEL
    if data.get("event") == "runtime.run":
        return (
            EmissionKind.MODEL,
            {
                "model": data.get("model", ""),
                "cost_usd": data.get("cost_usd", 0.0),
                "events": data.get("events", 0),
                "session_id": data.get("session_id"),
                "runtime": data.get("runtime", ""),
            },
            node_id,
        )

    # context.compaction -> COMPACTION
    if data.get("event") == "context.compaction":
        return (
            EmissionKind.COMPACTION,
            {
                "strategy": data.get("strategy", ""),
                "turns_before": data.get("turns_before"),
                "turns_after": data.get("turns_after"),
                "reclaimed_tokens": data.get("reclaimed_tokens"),
            },
            node_id,
        )

    # run-lifecycle spans -> RUN_START / RUN_FINISH
    if data.get("type") == "span":
        name = str(data.get("name") or "")
        if name == "run.finish":
            return (
                EmissionKind.RUN_FINISH,
                {
                    "status": data.get("status", "unknown"),
                    "cost_usd": data.get("cost_usd"),
                    "latency_ms": data.get("latency_ms"),
                    "reason": data.get("reason"),
                    "detail": data.get("detail"),
                },
                node_id,
            )
        if name == "run.suspended":
            return (
                EmissionKind.RUN_FINISH,
                {"status": "suspended", "reason": data.get("reason")},
                node_id,
            )
        # run.start (and any other span) -> RUN_START; carry the span name.
        return (
            EmissionKind.RUN_START,
            {
                "runtime": data.get("runtime", ""),
                "span": name,
                "definition": data.get("definition"),
            },
            node_id,
        )

    # sink.write -> SINK
    if data.get("type") == "sink.write":
        return (
            EmissionKind.SINK,
            {
                "target": data.get("sink", ""),
                "committed": True,
                "output_id": data.get("output_id"),
                "batch_id": data.get("batch_id"),
                "idempotency_key": data.get("idempotency_key"),
            },
            _opt_str(data.get("node_id")),
        )

    # ObserverEvent dumps (have pipeline + kind + severity) -> OBSERVER
    if "severity" in data and "kind" in data and "pipeline" in data:
        return (
            EmissionKind.OBSERVER,
            {
                "kind": data.get("kind", ""),
                "severity": data.get("severity", "info"),
                "detail": data.get("detail", ""),
                "observer": data.get("observer"),
            },
            _opt_str(data.get("observer")),
        )

    # Unknown legacy shape: lift into a generic METRIC carrying the raw payload,
    # so nothing on an old ledger is ever lost or raises on inspect.
    return (
        EmissionKind.METRIC,
        {"metric": "legacy_event", "value": 0, "raw": dict(data)},
        node_id,
    )


def emit(
    store: Store,
    e: Emission,
    *,
    org_id: str = "local",
    max_per_run: int | None = None,
) -> None:
    """Write a typed :class:`Emission` to the ledger via ``Store.append_event``.

    ``ScrubbingStore`` (when the store is wrapped) redacts secrets on the write —
    this never bypasses it. A lightweight per-run volume cap guards against an
    emission-flood DoS: if ``max_per_run`` is set and the run already holds at least
    that many events, the emission is dropped and a single capped-warning OBSERVER
    emission is written in its place (only the first time the cap is crossed).

    Determinism: ``ts`` is whatever the caller stamped on ``e`` (default ``0.0``);
    this path reads no wall clock.
    """
    # TODO(CRA-191): rotation/retention — this cap only drops, it does not rotate.
    if max_per_run is not None:
        existing = store.events(e.run_id, org_id=org_id)
        if len(existing) >= max_per_run:
            already_capped = any(
                ev.get("kind") == EmissionKind.OBSERVER.value
                and isinstance(ev.get("attrs"), dict)
                and ev["attrs"].get("kind") == "emission.capped"
                for ev in existing
            )
            if not already_capped:
                warning = Emission(
                    kind=EmissionKind.OBSERVER,
                    run_id=e.run_id,
                    org_id=org_id,
                    attrs={
                        "kind": "emission.capped",
                        "severity": "warn",
                        "cap": max_per_run,
                    },
                )
                store.append_event(e.run_id, warning.to_event(), org_id=org_id)
            return
    store.append_event(e.run_id, e.to_event(), org_id=org_id)


def read_emissions(store: Store, run_id: str, *, org_id: str = "local") -> list[Emission]:
    """Read a run's ledger and lift every event into a typed :class:`Emission`.

    Mixed ledgers work: legacy loose dicts lift via :meth:`Emission.from_event`'s
    back-compat shim, typed emissions round-trip exactly. Pure read — no clock.
    """
    rows = store.events(run_id, org_id=org_id)
    return [Emission.from_event(row) for row in rows]


# -- corrections (F-4) --------------------------------------------------------
def emit_correction(
    store: Store,
    *,
    run_id: str,
    correction_type: CorrectionType,
    provenance: Provenance,
    org_id: str = "local",
    tainted: bool = False,
    inputs: dict[str, JSONValue] | None = None,
    expected: JSONValue = None,
    produced: JSONValue = None,
    node_id: str | None = None,
    pipeline: str | None = None,
    ts: float = 0.0,
    attrs: dict[str, JSONValue] | None = None,
) -> Emission:
    """Record a ground-truth ``correction`` (F-4) onto the ledger AND the corpus.

    A correction names a wrong agent output discovered by a trusted source
    (``correction_type`` = how it was found). It is written twice:

    * as a typed :class:`Emission` of kind ``CORRECTION`` on the run's ledger (the
      taxonomy half — queryable per run, taint-propagating);
    * as a generic Store record under :data:`CORRECTION_RECORD_KIND` keyed by the
      emission id (the corpus half — listable/filterable across runs, org-isolated)
      via the existing record API (no Store-file edits; the F-2 pattern).

    SECURITY (Gap S4 — corpus poisoning): ``provenance`` declares WHO authored the
    correction. ``tainted`` propagates the fluid/untrusted marker. Neither is
    enforced here (every attempt is *recorded* for audit); the trust gate lives in
    :meth:`crawfish.eval.GoldenSet.from_corrections`, which admits only
    ``provenance == TRUSTED`` AND ``tainted is False`` corrections as ground truth
    and quarantines the rest. A correction derived from fluid input therefore can
    never silently become a guard's ground truth.

    Determinism: ``ts`` is whatever the caller stamps (default ``0.0``); no clock.
    """
    payload: dict[str, JSONValue] = {
        "correction_type": correction_type.value,
        "provenance": provenance.value,
    }
    if inputs is not None:
        payload["inputs"] = inputs
    if expected is not None:
        payload["expected"] = expected
    if produced is not None:
        payload["produced"] = produced
    if attrs:
        payload.update(attrs)

    em = Emission(
        kind=EmissionKind.CORRECTION,
        run_id=run_id,
        org_id=org_id,
        pipeline=pipeline,
        node_id=node_id,
        ts=ts,
        attrs=payload,
        tainted=tainted,
    )
    # Ledger half (per-run, taint-propagating). ScrubbingStore, if wrapping, redacts.
    store.append_event(run_id, em.to_event(), org_id=org_id)
    # Corpus half (cross-run, org-isolated, kind-filterable).
    store.put_record(CORRECTION_RECORD_KIND, em.id, em.to_event(), org_id=org_id)
    return em


def read_corrections(
    store: Store,
    *,
    org_id: str = "local",
    kinds: tuple[CorrectionType, ...] | None = None,
) -> list[Emission]:
    """List every recorded ``correction`` for ``org_id`` (cross-run), as Emissions.

    Reads the corpus half (the :data:`CORRECTION_RECORD_KIND` records), so this is a
    cross-run query — unlike :func:`read_emissions`, which is per-run. ``kinds``
    filters to specific :class:`CorrectionType` sub-categories. Org isolation holds:
    only this ``org_id``'s correction records are returned. Pure read — no clock.
    """
    wanted = {k.value for k in kinds} if kinds is not None else None
    out: list[Emission] = []
    for row in store.list_records(CORRECTION_RECORD_KIND, org_id=org_id):
        em = Emission.from_event(row)
        if em.kind is not EmissionKind.CORRECTION:
            continue
        if wanted is not None and em.attrs.get("correction_type") not in wanted:
            continue
        out.append(em)
    return out
