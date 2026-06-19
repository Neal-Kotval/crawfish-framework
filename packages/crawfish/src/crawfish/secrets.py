"""Credential resolution **by reference** (CRA-103/CRA-104, full model in CRA-114).

A node never stores a credential value — only a *reference* (an env-var name). It is
resolved at the egress boundary and never written to config, logs, Output, or the
prompt/transcript.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

__all__ = ["resolve_secret"]


def resolve_secret(ref: str | None, env: Mapping[str, str] | None = None) -> str | None:
    """Resolve a secret reference (env-var name) to its value, or None if unset."""
    if not ref:
        return None
    return (env or os.environ).get(ref)
