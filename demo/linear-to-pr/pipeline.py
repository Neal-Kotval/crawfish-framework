"""linear-to-pr — wire and run the pipeline end to end (dry-run, offline).

    Linear issues (Source) -> draft Markdown (Batch) -> open PR (Sink)

Each Linear issue fans out into its own Run; the ``pr-drafter`` agent drafts a Markdown
file from the issue; and ``MarkdownPRSink`` opens a pull request that adds that file on
the branch the ticket provides. Everything is dry-run and uses ``MockRuntime`` (no model
call, no network, no API key), so this is deterministic and free to run.

    uv run python demo/linear-to-pr/pipeline.py            # safe: fixtures + dry-run

To go live (real Linear issues, real GitHub PRs), set the creds by reference and pass
``--live``:

    export LINEAR_API_KEY=lin_api_...        # Linear personal API key
    export GITHUB_TOKEN=ghp_...              # repo-scoped token
    export CRAWFISH_PR_REPO=you/sandbox-repo # throwaway repo to open PRs against
    uv run python demo/linear-to-pr/pipeline.py --live
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from crawfish.batch import Batch
from crawfish.core import Flow, Parameter
from crawfish.core.context import RunContext
from crawfish.definition import Definition
from crawfish.runtime import MockRuntime
from crawfish.runtime.base import RunRequest
from crawfish.secrets import load_env
from crawfish.store import SqliteStore
from crawfish.triggers import IntervalTrigger
from crawfish.workflow import Workflow

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))  # allow `import markdown_pr` when run from the repo root

from markdown_pr import LINEAR_ISSUES, LinearIssueSource, MarkdownPRSink  # noqa: E402


def _load_dotenv() -> None:
    """Load creds from a gitignored `.env` (demo dir, then repo cwd) into the env.

    Uses ``setdefault`` so anything already exported on the shell wins over the file.
    """
    for path in (HERE / ".env", Path(".env")):
        for key, value in load_env(path).items():
            os.environ.setdefault(key, value)


def draft_markdown(request: RunRequest) -> str:
    """Deterministic stand-in for the pr-drafter agent.

    Renders the issue fields (all fluid/untrusted) into a Markdown body. The issue text
    is treated strictly as *data*: it is embedded, never interpreted as an instruction,
    which is why CRA-203's injection attempt has no effect on where the PR lands.
    """
    issue = request.inputs
    title = issue.get("title", "")
    identifier = issue.get("identifier", "")
    description = issue.get("description", "")
    return (
        f"# {title}\n\n"
        f"> Auto-drafted by crawfish for Linear issue **{identifier}**.\n\n"
        f"{description}\n"
    )


def assemble(live: bool) -> Workflow:
    """Build the Source -> Batch -> Sink Workflow. Shared by the CLI and `craw deploy`."""
    if live:
        repo = os.environ.get("CRAWFISH_PR_REPO", "")
        if not repo:
            raise SystemExit("set $CRAWFISH_PR_REPO=owner/repo (a throwaway repo) for live mode")
        source_config: dict = {"live": True, "linear_api_ref": "LINEAR_API_KEY", "limit": 10}
    else:
        repo = "acme/handbook"  # placeholder; dry-run never touches the network
        source_config = {"items": LINEAR_ISSUES}

    source = LinearIssueSource("linear-issues", config=source_config)
    drafter = Definition.from_package(str(HERE / "pr-drafter"))
    batch = Batch(drafter, name="draft-prs")
    sink = MarkdownPRSink(
        name="github-pr",
        config={"repo": repo, "base": "main", "credential_ref": "GITHUB_TOKEN"},
        # The egress target is static-only: a fluid `repo` would be rejected at
        # construction, so model output can never redirect where PRs land.
        target_params=[Parameter(name="repo", type="str", flow=Flow.STATIC)],
        dry_run=not live,
    )
    return Workflow(
        steps=[source, batch, sink],
        name="linear-to-pr",
        runtime=MockRuntime(responder=draft_markdown),
    )


# How this pipeline fires when deployed. `craw deploy` reads this when --schedule is
# omitted, so cadence is declared in the project, not on the command line. Every 30s is a
# fast, watch-it-tick cadence for development — pair it with CRAWFISH_DRY_RUN=1 (set in
# `.env`) so it never opens real PRs every 30s. For a live deployment use a calendar cron
# instead, e.g. CronTrigger("0 8 * * *") for daily at 08:00.
TRIGGER = IntervalTrigger(seconds=30)


def build_pipeline() -> Workflow:
    """Deployable factory discovered by `craw deploy` (deploy.load_workflow).

    Live by default *when configured*: if the creds are present (LINEAR_API_KEY,
    GITHUB_TOKEN, CRAWFISH_PR_REPO — e.g. in `.env`), a deploy fetches real Linear issues
    and opens real PRs, because that's what a deployed product is for. With no creds it
    degrades safely to fixtures + dry-run (so tests / CI / an unconfigured checkout never
    touch the network). Set ``CRAWFISH_DRY_RUN=1`` to force dry-run even when configured.
    """
    _load_dotenv()
    configured = all(
        os.environ.get(k) for k in ("LINEAR_API_KEY", "GITHUB_TOKEN", "CRAWFISH_PR_REPO")
    )
    forced_dry = os.environ.get("CRAWFISH_DRY_RUN") == "1"
    return assemble(live=configured and not forced_dry)


async def run(live: bool) -> None:
    wf = assemble(live)
    sink = wf.steps[-1]
    await wf.run(ctx=RunContext(store=SqliteStore()))

    repo = sink.config.get("repo")  # type: ignore[attr-defined]
    where = f"on {repo}" if live else "dry-run, no network"
    writes = sink.writes  # type: ignore[attr-defined]
    print(f"opened {len(writes)} pull request(s) — {where}:\n")
    for pr in writes:
        print(json.dumps(pr, indent=2, sort_keys=True))


def main() -> None:
    parser = argparse.ArgumentParser(description="Linear issues -> GitHub PRs")
    parser.add_argument(
        "--live",
        action="store_true",
        help="open REAL PRs from REAL Linear issues (needs LINEAR_API_KEY, GITHUB_TOKEN, "
        "CRAWFISH_PR_REPO). Without this flag the demo uses fixtures and dry-run.",
    )
    args = parser.parse_args()
    _load_dotenv()
    asyncio.run(run(args.live))


if __name__ == "__main__":
    main()
