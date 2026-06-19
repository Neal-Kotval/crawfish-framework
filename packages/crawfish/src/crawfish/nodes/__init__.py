"""IO nodes — Source (read), Sink (write), Filter (narrow/route) (M2)."""

from __future__ import annotations

from crawfish.nodes.filter import (
    Filter,
    field_equals,
    field_matches,
    limit,
    title_contains,
)
from crawfish.nodes.sink import (
    ApprovalRequired,
    GitHubPRSink,
    LinearSink,
    Sink,
    TargetMustBeStaticError,
)
from crawfish.nodes.source import PullRequestSource, RepoSource, Source, fan_out

__all__ = [
    # source
    "Source",
    "RepoSource",
    "PullRequestSource",
    "fan_out",
    # sink
    "Sink",
    "LinearSink",
    "GitHubPRSink",
    "TargetMustBeStaticError",
    "ApprovalRequired",
    # filter
    "Filter",
    "title_contains",
    "field_equals",
    "field_matches",
    "limit",
]
