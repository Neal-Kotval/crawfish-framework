"""Live HTTP clients for the linear-to-pr demo — stdlib only, no extra deps.

Two tiny clients over ``urllib``:

* :func:`fetch_linear_issues` — pull issues (and their ticket branch) from Linear's
  GraphQL API with a personal API key.
* :func:`open_pull_request` — open a GitHub PR that adds one Markdown file on a branch,
  using the REST API: create the branch ref, commit the file via the Contents API, then
  open the PR.

Tokens are passed in already-resolved (the callers resolve them by reference via
``crawfish.secrets.resolve_secret``); they are never logged here.
"""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request
from typing import Any

__all__ = ["fetch_linear_issues", "open_pull_request"]

_TIMEOUT = 30


def _request(
    url: str,
    *,
    headers: dict[str, str],
    method: str = "GET",
    data: dict[str, Any] | None = None,
    tolerate: tuple[int, ...] = (),
) -> Any:
    """Issue one JSON request. Raise on non-2xx unless the code is in ``tolerate``."""
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, method=method)
    for key, value in headers.items():
        req.add_header(key, value)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:  # noqa: S310 (trusted hosts)
            raw = resp.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        if exc.code in tolerate:
            return json.loads(raw) if raw else None
        # Surface the API's error body, but never the request headers (which hold creds).
        detail = raw.decode(errors="replace")
        raise RuntimeError(f"{method} {url} -> HTTP {exc.code}: {detail}") from None


# -- Linear -----------------------------------------------------------------------

_LINEAR_QUERY = """
query Issues($first: Int!) {
  issues(first: $first, filter: { state: { type: { neq: "completed" } } }) {
    nodes { identifier title description branchName }
  }
}
"""


def fetch_linear_issues(token: str, *, limit: int = 10) -> list[dict[str, Any]]:
    """Fetch open Linear issues as ``{identifier, title, description, branch}`` dicts.

    ``branch`` is Linear's ``branchName`` (the ticket's suggested git branch) — the head
    branch each PR will be opened on.
    """
    payload = _request(
        "https://api.linear.app/graphql",
        headers={"Authorization": token},  # personal API keys are sent raw, not Bearer
        method="POST",
        data={"query": _LINEAR_QUERY, "variables": {"first": limit}},
    )
    if not isinstance(payload, dict) or payload.get("errors"):
        raise RuntimeError(f"Linear API error: {payload}")
    nodes = payload["data"]["issues"]["nodes"]
    return [
        {
            "identifier": n["identifier"],
            "title": n.get("title") or "",
            "description": n.get("description") or "",
            "branch": n["branchName"],
        }
        for n in nodes
    ]


# -- GitHub -----------------------------------------------------------------------


def open_pull_request(
    *,
    repo: str,
    base: str,
    head: str,
    path: str,
    content: str,
    title: str,
    body: str,
    token: str,
) -> dict[str, Any]:
    """Open a PR that adds ``content`` at ``path`` on branch ``head``, targeting ``base``.

    Idempotent on re-run: an existing branch, file, or PR is tolerated rather than fatal.
    Returns the PR object (or the existing one) from the GitHub API.
    """
    api = f"https://api.github.com/repos/{repo}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "crawfish-linear-to-pr",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    # 1. Resolve the base branch's tip commit. A brand-new empty repo has no base
    #    branch yet (HTTP 409) — seed it with an initial commit, then resolve again.
    try:
        base_ref = _request(f"{api}/git/ref/heads/{base}", headers=headers)
    except RuntimeError as exc:
        if "409" not in str(exc) and "404" not in str(exc):
            raise
        _request(
            f"{api}/contents/README.md",
            headers=headers,
            method="PUT",
            data={
                "message": "Initialize repository",
                "content": base64.b64encode(
                    f"# {repo.split('/')[-1]}\n\nSeeded by the crawfish linear-to-pr demo.\n".encode()  # noqa: E501
                ).decode(),
                "branch": base,
            },
        )
        base_ref = _request(f"{api}/git/ref/heads/{base}", headers=headers)
    base_sha = base_ref["object"]["sha"]

    # 2. Create the head branch from base (tolerate 422 == already exists).
    _request(
        f"{api}/git/refs",
        headers=headers,
        method="POST",
        data={"ref": f"refs/heads/{head}", "sha": base_sha},
        tolerate=(422,),
    )

    # 3. Commit the Markdown file onto the head branch. If it already exists on that
    #    branch, the Contents API needs its blob sha — fetch and retry once.
    put: dict[str, Any] = {
        "message": title,
        "content": base64.b64encode(content.encode()).decode(),
        "branch": head,
    }
    result = _request(
        f"{api}/contents/{path}", headers=headers, method="PUT", data=put, tolerate=(422,)
    )
    if isinstance(result, dict) and "commit" not in result:
        existing = _request(f"{api}/contents/{path}?ref={head}", headers=headers, tolerate=(404,))
        if isinstance(existing, dict) and existing.get("sha"):
            put["sha"] = existing["sha"]
            _request(f"{api}/contents/{path}", headers=headers, method="PUT", data=put)

    # 4. Open the PR (tolerate 422 == one already open for this head).
    pr = _request(
        f"{api}/pulls",
        headers=headers,
        method="POST",
        data={"title": title, "head": head, "base": base, "body": body},
        tolerate=(422,),
    )
    if isinstance(pr, dict) and pr.get("html_url"):
        return pr
    # A 422 means a PR for this head already exists — return it instead.
    existing_prs = _request(
        f"{api}/pulls?head={repo.split('/')[0]}:{head}&state=open", headers=headers
    )
    if isinstance(existing_prs, list) and existing_prs:
        return existing_prs[0]
    return pr if isinstance(pr, dict) else {}
