"""The ``craw`` CLI (M0 bootstrap).

Full command surface — ``init / install / list / run / freeze / publish / dev /
build / test / logs / inspect`` — lands in CRA-113, CRA-115, CRA-119, CRA-120.
M0 ships ``--version`` and a ``run`` that drives the engine bootstrap so an empty
project's no-op pipeline runs end to end.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from importlib.metadata import version as _pkg_version

from crawfish.engine import run_pipeline


def _version() -> str:
    try:
        return _pkg_version("crawfish")
    except Exception:  # pragma: no cover - source checkout without install
        return "0.0.0+dev"


def _cmd_run(_args: argparse.Namespace) -> int:
    # M0: an empty project compiles to a no-op pipeline. CRA-109/CRA-113 wire
    # real project loading + a typed Workflow here.
    outputs = asyncio.run(run_pipeline([]))
    print(f"pipeline ok: {len(outputs)} output(s)")
    return 0


def _cmd_dev(args: argparse.Namespace) -> int:
    """Fast dev loop: compile a Definition directory and run it on the mock runtime
    (zero key, zero budget) — the basis for fixtures + record/replay hot-reload."""
    from crawfish.core.context import RunContext
    from crawfish.definition import Definition
    from crawfish.runtime import MockRuntime, run_team
    from crawfish.store import SqliteStore

    definition = Definition.from_package(args.path)
    inputs: dict[str, object] = {}
    for pair in args.input or []:
        key, _, value = pair.partition("=")
        inputs[key] = value

    async def _go() -> str:
        ctx = RunContext(store=SqliteStore())
        # Mock runtime + the team coordinator: zero key, zero budget, exercises the
        # full coordination topology. CRA-112's record/replay swaps in for real runs.
        result = await run_team(definition, inputs, ctx, MockRuntime())
        return result.text

    print(asyncio.run(_go()))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="craw", description="Crawfish CLI")
    parser.add_argument("--version", action="version", version=f"crawfish {_version()}")
    sub = parser.add_subparsers(dest="command")
    run_p = sub.add_parser("run", help="run the project's pipeline")
    run_p.set_defaults(func=_cmd_run)
    dev_p = sub.add_parser("dev", help="compile + run a Definition on the mock runtime")
    dev_p.add_argument("path", help="path to a Definition directory")
    dev_p.add_argument("-i", "--input", action="append", help="input as name=value (repeatable)")
    dev_p.set_defaults(func=_cmd_dev)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    result: int = args.func(args)
    return result


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
