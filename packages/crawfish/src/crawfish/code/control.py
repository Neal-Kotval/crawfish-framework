"""``craw code cancel <run_id>`` / ``craw code resume <run_id>`` — the control plane.

The operate plane's *control* verbs (UNFILED-CONTROL, M4.5). They are thin orchestrators
over the **shipped** cooperative-cancellation + ledger-resume primitives — they reinvent
neither:

* **cancel** — cooperative cancellation over the shipped
  :class:`~crawfish.core.context.CancelToken`. For an in-process run a caller hands its own
  token (long loops already call ``raise_if_cancelled``); for a *deployed* run the verb
  signals the supervisor via the existing :func:`crawfish.deploy.stop` / registry path. It
  is **never** a hard kill of host code — cancellation is cooperative by construction
  (SECURITY.md "no signal into out-of-process host code beyond the supervisor stop").
* **resume** — re-enters the ledger resume path (the same skip-DONE the Supervisor's
  ``process_items`` uses): completed loop iterations re-charge **$0**. Tenancy folds into the
  ledger key, so a resume in org A never replays org B's completed work (SECURITY.md
  "Tenancy and run identity"). A resume whose remaining work could fire a consequential
  ``--live`` sink is the HITL gate's concern (M6) — this verb only re-enters the durable
  resume path; it fires no Sink itself.

Both emit the versioned ``craw.code.control.v1`` envelope and the closed exit-code table
(``0`` ok, ``1`` no such run, ``6`` cancel raced a completed run / no-op). Protocols only:
the Store is opened through :func:`crawfish.manage.store_for_dir`; no concrete backend is
named here.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import TYPE_CHECKING

from crawfish.code import (
    EXIT_OK,
    SCHEMA_VERSIONS,
    ErrorCode,
    emit_error,
    emit_json,
)

if TYPE_CHECKING:
    from crawfish.core.context import CancelToken
    from crawfish.store.base import Store

# Self-register the verb's --json schema major (CRA-269). ``setdefault`` never clobbers an
# existing entry, so a re-import (test re-discovery) is idempotent.
SCHEMA_VERSIONS.setdefault("code.control", (1, 0))  # type: ignore[attr-defined]

VERB_NAME = "control"  # module name; this file registers TWO verbs (cancel + resume)

#: The run id is absent from this org's ledger/surface.
EXIT_NO_SUCH_RUN = 1
#: Cancel raced a run that already finished (a cooperative no-op, not a failure).
EXIT_RACED_DONE = 6


class NoSuchRun(LookupError):
    """The ``run_id`` is not in this org's ledger/surface (exit ``1``)."""


def register(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    """Register both ``craw code cancel`` and ``craw code resume`` (self-registering)."""
    from crawfish.code.cli import add_common_args

    cancel_p = subparsers.add_parser(
        "cancel", help="cooperatively cancel a running pipeline (UNFILED-CONTROL)"
    )
    cancel_p.add_argument("run_id", help="the run id to cancel")
    add_common_args(cancel_p)
    cancel_p.set_defaults(func=_cmd_cancel)

    resume_p = subparsers.add_parser(
        "resume", help="resume a durable run from its ledger checkpoint, $0 (UNFILED-CONTROL)"
    )
    resume_p.add_argument("run_id", help="the run id to resume")
    add_common_args(resume_p)
    resume_p.set_defaults(func=_cmd_resume)


# --------------------------------------------------------------------------- cancel


def cancel_run(
    run_id: str,
    *,
    store: Store,
    org_id: str = "local",
    token: CancelToken | None = None,
    stop_supervisor: bool = True,
) -> dict[str, object]:
    """Cooperatively cancel a run; return the ``craw.code.control.v1`` body.

    Cancellation is **cooperative** (never a hard kill): an in-process ``token`` is set
    (long loops poll ``raise_if_cancelled``); a deployed run is signalled through the
    shipped supervisor ``stop`` path keyed on its pipeline name. A run already ``done`` /
    ``failed`` is a **no-op** (``result="raced_done"``) — there is nothing left to cancel.

    Raises :class:`NoSuchRun` when the ``run_id`` is absent from this org's surface.
    """
    from crawfish.observe import ObserverSurface

    surface = ObserverSurface(store, org_id=org_id)
    info = surface.get_run_info(run_id)
    if info is None:
        raise NoSuchRun(run_id)

    # A run that already terminated cannot be cancelled — surface the race as a no-op so an
    # agent does not loop on it (exit 6).
    if info.status in ("done", "failed", "cancelled"):
        return {
            "run_id": run_id,
            "action": "cancel",
            "result": "raced_done",
            "items_replayed_free": 0,
            "items_remaining": 0,
            "recharged_usd": 0.0,
        }

    # In-process: set the caller's cooperative token (the only thing that touches a live run).
    if token is not None:
        token.cancel()

    # Deployed: signal the supervisor through the shipped registry ``stop`` path — never a
    # signal into arbitrary host code, only the supervisor's own cooperative shutdown.
    signalled = False
    if stop_supervisor:
        from crawfish.deploy import stop

        signalled = stop(info.pipeline, store=store, org_id=org_id)

    return {
        "run_id": run_id,
        "action": "cancel",
        "result": "cancelled" if (token is not None or signalled) else "signalled_none",
        "items_replayed_free": 0,
        "items_remaining": 0,
        "recharged_usd": 0.0,
    }


def _cmd_cancel(args: argparse.Namespace) -> int:
    """``craw code cancel <run_id> [--org ID] [--json]`` — cooperative cancellation."""
    org = getattr(args, "org", "local")
    as_json = getattr(args, "as_json", False)
    store = _store_for_cwd()
    try:
        body = cancel_run(args.run_id, store=store, org_id=org)
    except NoSuchRun:
        emit_error(
            ErrorCode.NOT_FOUND,
            remediation=f"No run {args.run_id!r} in org {org!r}; check `craw code fleet`.",
            detail={"run_id": args.run_id},
            as_json=as_json,
        )
        return EXIT_NO_SUCH_RUN
    finally:
        store.close()

    if as_json:
        emit_json("code.control", body, org=org)
    else:
        _print_human(body)
    return EXIT_RACED_DONE if body["result"] == "raced_done" else EXIT_OK


# --------------------------------------------------------------------------- resume


def resume_run(run_id: str, *, store: Store, org_id: str = "local") -> dict[str, object]:
    """Re-enter the ledger resume path for ``run_id``; return the control envelope body.

    Counts the run's pipeline's already-``DONE`` loop items from the **execution ledger**
    (``completed_items`` — the same skip-DONE the Supervisor's ``process_items`` honours):
    those re-charge **$0** on resume. ``items_remaining`` is the declared item count minus
    the completed set. Tenancy folds into the ledger ``org_id``, so a resume in one org never
    counts another org's completed iterations.

    Raises :class:`NoSuchRun` when the ``run_id`` is absent from this org's surface.
    """
    from crawfish.ledger import ExecutionLedger
    from crawfish.observe import ObserverSurface

    surface = ObserverSurface(store, org_id=org_id)
    info = surface.get_run_info(run_id)
    if info is None:
        raise NoSuchRun(run_id)

    ledger = ExecutionLedger(store, org_id=org_id)
    # Completed loop items for THIS run's pipeline, scoped to THIS org (cross-tenant resume
    # cannot see another org's DONE rows — the ledger read is org-keyed).
    done = ledger.completed_items(info.pipeline)
    replayed_free = len(done)
    remaining = max(0, info.items - replayed_free)

    return {
        "run_id": run_id,
        "action": "resume",
        "result": "resumed",
        "items_replayed_free": replayed_free,
        "items_remaining": remaining,
        # Completed iterations re-charge nothing — the load-bearing $0-resume invariant.
        "recharged_usd": 0.0,
    }


def _cmd_resume(args: argparse.Namespace) -> int:
    """``craw code resume <run_id> [--org ID] [--json]`` — durable, $0-on-DONE resume."""
    org = getattr(args, "org", "local")
    as_json = getattr(args, "as_json", False)
    store = _store_for_cwd()
    try:
        body = resume_run(args.run_id, store=store, org_id=org)
    except NoSuchRun:
        emit_error(
            ErrorCode.NOT_FOUND,
            remediation=f"No run {args.run_id!r} in org {org!r}; check `craw code fleet`.",
            detail={"run_id": args.run_id},
            as_json=as_json,
        )
        return EXIT_NO_SUCH_RUN
    finally:
        store.close()

    if as_json:
        emit_json("code.control", body, org=org)
    else:
        _print_human(body)
    return EXIT_OK


# --------------------------------------------------------------------------- shared


def _store_for_cwd() -> Store:
    """Open the per-project Store through the protocol-returning factory (never a backend).

    Control acts over the current project's ledger; :func:`crawfish.manage.store_for_dir`
    returns the ``Store`` *protocol*, so this module never names a concrete backend.
    """
    from crawfish.manage import store_for_dir

    Path(".crawfish").mkdir(parents=True, exist_ok=True)
    return store_for_dir(".")


def _print_human(body: dict[str, object]) -> None:
    """Human one-liner for cancel/resume (unchanged behaviour without ``--json``)."""
    action, result = body["action"], body["result"]
    if action == "resume":
        print(
            f"resume {body['run_id']}: {result} — "
            f"{body['items_replayed_free']} replayed free, "
            f"{body['items_remaining']} remaining (${body['recharged_usd']:.2f} recharged)"
        )
    else:
        print(f"cancel {body['run_id']}: {result}")
