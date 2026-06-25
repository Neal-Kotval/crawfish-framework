"""``craw code`` — the agent-authoring verb family + its shared CLI contracts.

This subpackage houses the whole ``craw code`` namespace (the LLM-in-the-author's-chair
surface). It is built around three contracts every verb shares, so a new verb is added as
a *file*, never by editing a shared dispatcher:

* **Exit codes** (CRA-243) — a uniform, documented table (:data:`EXIT_OK` …
  :data:`EXIT_SECURITY`) every verb returns through.
* **``--json`` schema-version negotiation** (CRA-269) — per-command
  ``schema_major.schema_minor`` in :data:`SCHEMA_VERSIONS`, with :func:`schema_tag` /
  :func:`schema_version` and the forward-compatible :func:`negotiate` (a major mismatch
  surfaces a ``schema_skew`` error, never a parse crash).
* **The ``craw.error.v1`` envelope** (CRA-270) — a single structured, recoverable error
  (:class:`ErrorEnvelope` / :func:`emit_error`) with a **closed** ``code`` enum;
  every *security* rejection is ``retryable=false`` (fail closed) and the static
  ``remediation`` never echoes fluid/tainted input back to the agent.

It also re-exports the CRA-266 provenance record format (the ``craw.code.provenance.v1``
projection) and a :data:`REGISTRY` that auto-discovers ``register(subparsers)`` hooks in
sibling modules (``pkgutil.iter_modules``), so future verbs self-register.
"""

from __future__ import annotations

import importlib
import json
import pkgutil
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    import argparse

    from crawfish.provenance import FileProvenance

__all__ = [
    # exit codes (CRA-243)
    "EXIT_OK",
    "EXIT_EXPECTED_FAILURE",
    "EXIT_USAGE",
    "EXIT_BUDGET",
    "EXIT_SECURITY",
    "EXIT_CODES",
    # schema negotiation (CRA-269)
    "SCHEMA_VERSIONS",
    "schema_tag",
    "schema_version",
    "negotiate",
    "SchemaSkew",
    # error envelope (CRA-270)
    "ErrorCode",
    "SECURITY_CODES",
    "ErrorEnvelope",
    "emit_error",
    "CODE_EXIT",
    # provenance projection (CRA-266)
    "provenance_payload",
    # registry (the self-registering verb discovery)
    "REGISTRY",
    "RegisterHook",
    "discover_verbs",
    "emit_json",
]


# ============================================================================
# CRA-243 — the uniform exit-code table (shared across every craw verb).
# ----------------------------------------------------------------------------
# ``craw code`` drives the project by parsing ``craw … --json`` over Bash, so every verb
# must return a *meaningful* exit code the agent can branch on. This is the canonical
# table; the CRA-270 envelope's ``CODE_EXIT`` maps each error code onto it.
EXIT_OK = 0  # success
EXIT_EXPECTED_FAILURE = 1  # expected failure (regression gate tripped, consent declined)
EXIT_USAGE = 2  # usage / compile error (bad args, DefinitionLoadError, jail Denial)
EXIT_BUDGET = 3  # budget exceeded (a --budget ceiling halted the run)
EXIT_SECURITY = 4  # security rejection (assembly gate, fluid→sink, signing) — non-retryable

#: The documented table, by name (snapshot-tested + rendered into the coverage matrix).
EXIT_CODES: Mapping[str, int] = {
    "ok": EXIT_OK,
    "expected_failure": EXIT_EXPECTED_FAILURE,
    "usage": EXIT_USAGE,
    "budget_exceeded": EXIT_BUDGET,
    "security_rejection": EXIT_SECURITY,
}


# ============================================================================
# CRA-269 — --json schema-version negotiation between plugin and CLI.
# ----------------------------------------------------------------------------
# Per-command ``(major, minor)``. A **major** bump is breaking (field removed / re-typed);
# a **minor** bump is additive (new field, old parsers ignore it). The plugin declares the
# majors it understands; the CLI advertises what it emits; a mismatch is a structured
# ``schema_skew`` (CRA-270), never a parse crash.
SCHEMA_VERSIONS: Mapping[str, tuple[int, int]] = {
    "code.provenance": (1, 0),
    "code.describe": (1, 0),
    "code.estimate": (1, 0),
    "code.schema": (1, 0),
    "code.run": (1, 0),
    "code.sync": (1, 0),
    "error": (1, 0),
}


class SchemaSkew(RuntimeError):
    """Raised when a plugin's understood major does not match the CLI's emitted major.

    Fail closed (SECURITY.md): a skew must not degrade to an unparsed/guessed payload that
    could mask a security field (a missing ``flow`` or ``tainted``). Carries the command +
    both majors so the CLI can build the ``schema_skew`` envelope.
    """

    def __init__(self, command: str, cli_major: int, plugin_major: int) -> None:
        self.command = command
        self.cli_major = cli_major
        self.plugin_major = plugin_major
        super().__init__(
            f"schema_skew on {command!r}: CLI emits major {cli_major}, plugin understands "
            f"major {plugin_major}"
        )


def schema_version(command: str) -> dict[str, int]:
    """The ``{"major": M, "minor": N}`` the CLI emits for ``command``."""
    major, minor = SCHEMA_VERSIONS.get(command, (1, 0))
    return {"major": major, "minor": minor}


def schema_tag(command: str) -> str:
    """The major-only tag string, e.g. ``"craw.code.describe.v1"``.

    The ``schema`` field a parser keys off — major only, so a minor (additive) bump keeps
    the tag stable and old parsers still match.
    """
    major = SCHEMA_VERSIONS.get(command, (1, 0))[0]
    return f"craw.{command}.v{major}"


def negotiate(command: str, plugin_major: int) -> dict[str, int]:
    """Forward-compatible compat check: the CLI's version, or raise :class:`SchemaSkew`.

    A plugin built for ``plugin_major`` calling ``command`` passes iff the CLI emits the
    same major (a minor bump is additive — the plugin ignores the new field). A major
    mismatch raises :class:`SchemaSkew`, which the CLI surfaces as a non-retryable
    ``schema_skew`` envelope (exit ``4``).
    """
    cli_major, cli_minor = SCHEMA_VERSIONS.get(command, (1, 0))
    if plugin_major != cli_major:
        raise SchemaSkew(command, cli_major, plugin_major)
    return {"major": cli_major, "minor": cli_minor}


# ============================================================================
# CRA-270 — the craw.error.v1 structured, recoverable error envelope.
# ----------------------------------------------------------------------------
class ErrorCode(str, Enum):
    """The **closed** error-code enum. ``(str, Enum)`` per ADR 0004 (UP042 disabled).

    Every agent-facing failure maps to exactly one. The *security* subset
    (:data:`SECURITY_CODES`) is always ``retryable=false`` — an injected agent must not be
    able to "retry past" a security gate.
    """

    USAGE = "usage"
    NOT_FOUND = "not_found"
    COMPILE_ERROR = "compile_error"
    JAIL_VIOLATION = "jail_violation"
    BUDGET_EXCEEDED = "budget_exceeded"
    SCHEMA_SKEW = "schema_skew"
    FLUID_TO_STATIC_SINK = "fluid_to_static_sink"
    SIGNING_REQUIRED = "signing_required"
    CONSENT_REQUIRED = "consent_required"
    # M6 HITL gate (UNFILED-GATE): a consequential promotion/--live call with no recorded
    # human approval, or one halted by the aggregate budget ceiling. Both are security
    # rejections (an injected agent must not retry past them); the spec's granular 7/8 codes
    # ride in the envelope ``detail["exit"]`` while the closed CRA-243 exit stays 4.
    NO_APPROVAL = "no_approval"
    CEILING_REACHED = "ceiling_reached"
    TREE_BUSY = "tree_busy"
    PLUGIN_SKEW = "plugin_skew"
    INTERNAL = "internal"


#: The security rejections — always non-retryable (fail closed).
SECURITY_CODES: frozenset[ErrorCode] = frozenset(
    {
        ErrorCode.JAIL_VIOLATION,
        ErrorCode.FLUID_TO_STATIC_SINK,
        ErrorCode.SIGNING_REQUIRED,
        ErrorCode.CONSENT_REQUIRED,
        ErrorCode.SCHEMA_SKEW,
        ErrorCode.NO_APPROVAL,
        ErrorCode.CEILING_REACHED,
    }
)

#: Each error code's exit code (CRA-243 table). Security → 4, budget → 3, the compile /
#: usage family → 2; not_found is a usage-class compile failure (2); internal → 2.
CODE_EXIT: Mapping[ErrorCode, int] = {
    ErrorCode.USAGE: EXIT_USAGE,
    ErrorCode.NOT_FOUND: EXIT_USAGE,
    ErrorCode.COMPILE_ERROR: EXIT_USAGE,
    ErrorCode.JAIL_VIOLATION: EXIT_SECURITY,
    ErrorCode.BUDGET_EXCEEDED: EXIT_BUDGET,
    ErrorCode.SCHEMA_SKEW: EXIT_SECURITY,
    ErrorCode.FLUID_TO_STATIC_SINK: EXIT_SECURITY,
    ErrorCode.SIGNING_REQUIRED: EXIT_SECURITY,
    ErrorCode.CONSENT_REQUIRED: EXIT_SECURITY,
    ErrorCode.NO_APPROVAL: EXIT_SECURITY,
    ErrorCode.CEILING_REACHED: EXIT_SECURITY,
    ErrorCode.TREE_BUSY: EXIT_EXPECTED_FAILURE,
    ErrorCode.PLUGIN_SKEW: EXIT_EXPECTED_FAILURE,
    ErrorCode.INTERNAL: EXIT_USAGE,
}


@dataclass(frozen=True)
class ErrorEnvelope:
    """The ``craw.error.v1`` envelope — a structured, recoverable error the agent reads.

    ``retryable`` is forced ``False`` for every :data:`security code <SECURITY_CODES>` (an
    injected agent must not retry past a security gate). ``remediation`` is a **static**
    human string: it must never echo fluid/tainted input (a tainted ticket body must not
    round-trip through an error message back into the agent's instruction stream).
    """

    code: ErrorCode
    remediation: str
    retryable: bool = False
    detail: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Security rejections are non-retryable, period — overriding a caller's mistake.
        if self.code in SECURITY_CODES and self.retryable:
            object.__setattr__(self, "retryable", False)

    @property
    def exit_code(self) -> int:
        return CODE_EXIT.get(self.code, EXIT_USAGE)

    def to_payload(self) -> dict[str, object]:
        """The JSON object emitted on stderr (sorted, versioned per CRA-269)."""
        return {
            "schema": schema_tag("error"),
            "schema_version": schema_version("error"),
            "code": self.code.value,
            "retryable": self.retryable,
            "detail": self.detail,
            "remediation": self.remediation,
        }


def emit_error(
    code: str | ErrorCode,
    *,
    retryable: bool = False,
    remediation: str,
    detail: dict[str, object] | None = None,
    as_json: bool = True,
    stream: object | None = None,
) -> int:
    """Print the ``craw.error.v1`` envelope to stderr (``--json``) and return the exit code.

    In ``--json`` mode the structured envelope goes to stderr (never a raw traceback); in
    human mode a clean one-line message is printed. Returns the CRA-243 exit code for the
    error, so a CLI verb does ``return emit_error(...)``.
    """
    env = ErrorEnvelope(
        code=ErrorCode(code) if not isinstance(code, ErrorCode) else code,
        remediation=remediation,
        retryable=retryable,
        detail=detail or {},
    )
    out = stream if stream is not None else sys.stderr
    if as_json:
        print(json.dumps(env.to_payload(), sort_keys=True), file=out)  # type: ignore[arg-type]
    else:
        tag = "non-retryable" if not env.retryable else "retryable"
        print(f"error [{env.code.value}, {tag}]: {env.remediation}", file=out)  # type: ignore[arg-type]
    return env.exit_code


# ============================================================================
# CRA-266 — the per-file provenance projection (craw.code.provenance.v1).
# ----------------------------------------------------------------------------
def provenance_payload(prov: FileProvenance) -> dict[str, object]:
    """Project a :class:`~crawfish.provenance.FileProvenance` to its versioned ``--json``.

    The shape ``craw code describe`` / ``sync`` consume so the agent knows a file's trust
    (``authored_by`` / ``source_tainted`` / ``taint``). Typed-shape-only — no secrets.
    """
    return {
        "schema": schema_tag("code.provenance"),
        "schema_version": schema_version("code.provenance"),
        "component": prov.component_path,
        "content_sha": prov.content_sha,
        "authored_by": prov.authored_by,
        "source_tainted": prov.source_tainted,
        "taint": sorted(prov.taint),
    }


# ============================================================================
# The self-registering verb registry (architectural decision #2).
# ----------------------------------------------------------------------------
class RegisterHook(Protocol):
    """A sibling module's ``register(subparsers)`` hook contract.

    A new ``craw code`` verb is a file in this subpackage exposing
    ``def register(subparsers: argparse._SubParsersAction) -> None``. :data:`REGISTRY`
    discovers it via ``pkgutil.iter_modules`` — no shared dispatcher is edited to add a
    verb.
    """

    def __call__(self, subparsers: argparse._SubParsersAction) -> None: ...  # type: ignore[type-arg]


def discover_verbs() -> list[RegisterHook]:
    """Find every sibling module exposing a ``register(subparsers)`` hook.

    Walks this package's modules with ``pkgutil.iter_modules`` and collects each module's
    ``register`` callable, sorted by module name (deterministic CLI ordering). ``cli`` and
    this ``__init__`` are skipped (they wire the group, they are not verbs).
    """
    hooks: list[tuple[str, RegisterHook]] = []
    for info in pkgutil.iter_modules(__path__):
        if info.name in ("cli", "__init__"):
            continue
        module = importlib.import_module(f"{__name__}.{info.name}")
        hook = getattr(module, "register", None)
        if callable(hook):
            hooks.append((info.name, hook))
    return [hook for _name, hook in sorted(hooks)]


#: The lazily-populated verb registry. ``cli.build_code_parser`` calls
#: :func:`discover_verbs` once; exposed as a name so tests can introspect it.
REGISTRY: list[RegisterHook] = []


def emit_json(command: str, payload: dict[str, object], *, org: str = "local") -> None:
    """The single ``--json`` emitter for a ``craw code`` verb (CRA-243).

    Wraps ``payload`` in the negotiated envelope (``schema`` major tag + ``schema_version``
    + ``org``), ``sort_keys=True`` — generalizing the optimization plane's ``_opt_print``
    envelope to every verb. The verb passes only its body; the schema/version/org header is
    added here so no verb re-implements the envelope.
    """
    envelope: dict[str, object] = {
        "schema": schema_tag(command),
        "schema_version": schema_version(command),
        "org": org,
    }
    envelope.update(payload)
    print(json.dumps(envelope, sort_keys=True))
