"""The ``craw code`` argparse subcommand group + registry dispatch.

This is the *only* place the ``craw code`` verb group is assembled. It does **not**
hard-code the verb list: it calls :func:`~crawfish.code.discover_verbs` and lets each
sibling module register itself (architectural decision #2), so a new verb is a new file,
never an edit here. The top-level ``crawfish.cli`` wires ``code`` in once via
:func:`register_code_command`.

Every verb shares the CRA-243 ``--json`` / exit-code contract and the CRA-270 error
envelope; the shared flags (``--json`` / ``--org``) are attached by :func:`add_common_args`
so a verb opts in uniformly.
"""

from __future__ import annotations

import argparse

from crawfish.code import REGISTRY, discover_verbs

__all__ = [
    "add_common_args",
    "build_code_parser",
    "register_code_command",
    "run_code",
]


def add_common_args(parser: argparse.ArgumentParser) -> None:
    """Attach the flags every ``craw code`` verb shares (CRA-243 / CRA-275).

    ``--json`` selects the versioned machine-readable envelope (the surface ``craw code``
    parses); ``--org`` threads the tenancy ``org_id`` to every Store read/write.
    """
    parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="emit the versioned craw.<cmd>.v<N> --json envelope",
    )
    parser.add_argument(
        "--org", default="local", help="tenancy org_id threaded to every Store read/write"
    )


def build_code_parser(subparsers: argparse._SubParsersAction) -> argparse.ArgumentParser:  # type: ignore[type-arg]
    """Build the ``code`` subcommand group and let each verb self-register.

    Returns the ``code`` parser (with its own nested subparsers). Populates the module
    :data:`~crawfish.code.REGISTRY` from :func:`~crawfish.code.discover_verbs` so tests can
    introspect what registered.
    """
    code_parser: argparse.ArgumentParser = subparsers.add_parser(
        "code", help="agent-authoring verbs (craw code <verb>)"
    )
    code_sub = code_parser.add_subparsers(dest="code_command")
    REGISTRY.clear()
    for hook in discover_verbs():
        REGISTRY.append(hook)
        hook(code_sub)
    code_parser.set_defaults(func=_code_dispatch)
    return code_parser


def _code_dispatch(args: argparse.Namespace) -> int:
    """Dispatch a ``craw code`` invocation to the selected verb's ``func``.

    Each verb sets its own ``func`` via ``set_defaults`` during ``register``; a bare
    ``craw code`` (no verb) prints help and returns ``0``.
    """
    verb_func = getattr(args, "func", None)
    if verb_func is None or verb_func is _code_dispatch:
        # No verb selected — surface the group help (no error envelope; this is usage).
        print("usage: craw code <verb> [--json] [--org ID]; verbs:", end=" ")
        print(", ".join(_registered_verb_names()))
        return 0
    result: int = verb_func(args)
    return result


def _registered_verb_names() -> list[str]:
    """The verb names currently registered (for the bare-`craw code` help line)."""
    import importlib
    import pkgutil

    import crawfish.code as code_pkg

    names: list[str] = []
    for info in pkgutil.iter_modules(code_pkg.__path__):
        if info.name in ("cli", "__init__"):
            continue
        module = importlib.import_module(f"crawfish.code.{info.name}")
        verb = getattr(module, "VERB_NAME", info.name)
        names.append(str(verb))
    return sorted(names)


def register_code_command(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    """Wire the ``code`` group into a top-level ``craw`` subparsers object (called once)."""
    build_code_parser(subparsers)


def run_code(argv: list[str] | None = None) -> int:
    """Standalone entry: parse + dispatch a ``craw code …`` argv (used by tests).

    Builds a throwaway top-level parser with just the ``code`` group so a test can drive a
    verb without the whole ``craw`` surface.
    """
    parser = argparse.ArgumentParser(prog="craw")
    sub = parser.add_subparsers(dest="command")
    register_code_command(sub)
    args = parser.parse_args(["code", *(argv or [])])
    func = getattr(args, "func", None)
    if func is None:
        parser.parse_args(["code"])
        return 0
    result: int = func(args)
    return result
