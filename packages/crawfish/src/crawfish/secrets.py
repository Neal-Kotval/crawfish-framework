"""Secrets v1 + hardening.

Credentials are held **by reference** (an env-var name), never by value: a node
receives only the secrets it declares (least privilege — the embryonic capability
manifest), the value never reaches stored config, an Output, logs, or the prompt.
Transcripts/telemetry are **scrubbed before the Store write** (:class:`ScrubbingStore`).
A package's declared capabilities are surfaced at install time for consent.

Known v1 tradeoff (see SECURITY.md): a local CommandRuntime can read ``.env`` in its
sandbox; closed later by egress-mediated injection. Out-of-process host-side execution
+ taint propagation are the runtime half of this hardening.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from crawfish.core.ids import new_id
from crawfish.core.types import JSONValue
from crawfish.store.base import Store

__all__ = [
    "resolve_secret",
    "load_env",
    "SecretManager",
    "redact",
    "redact_obj",
    "ScrubbingStore",
    "read_capabilities",
    "Capabilities",
    "Grant",
]

# Heuristic patterns for common credentials/PII, scrubbed even if not in the env map.
_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9]{16,}"),
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._-]{12,}"),
    re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"),  # email (PII)
]
_REDACTED = "***REDACTED***"


def resolve_secret(ref: str | None, env: Mapping[str, str] | None = None) -> str | None:
    """Resolve a secret reference (env-var name) to its value, or None if unset."""
    if not ref:
        return None
    if env is not None:
        return env.get(ref)
    import os

    return os.environ.get(ref)


def load_env(path: str | Path = ".env") -> dict[str, str]:
    """Parse a gitignored ``.env`` (KEY=VALUE lines). Values are never logged."""
    p = Path(path)
    if not p.exists():
        return {}
    env: dict[str, str] = {}
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


class SecretManager:
    """Maps nodes to the secrets they declare and resolves them least-privilege."""

    def __init__(self, env: Mapping[str, str] | None = None) -> None:
        self._env = dict(env) if env is not None else load_env()
        self._declared: dict[str, set[str]] = {}

    def declare(self, node_id: str, refs: Iterable[str]) -> None:
        self._declared.setdefault(node_id, set()).update(r for r in refs if r)

    def for_node(self, node_id: str) -> dict[str, str]:
        """Return only the secrets this node declared (and that exist)."""
        return {
            ref: self._env[ref] for ref in self._declared.get(node_id, set()) if ref in self._env
        }

    @property
    def values(self) -> list[str]:
        """All known secret values (for scrubbing)."""
        return [v for v in self._env.values() if v]


def redact(text: str, secrets: Iterable[str] = ()) -> str:
    """Replace known secret values and credential/PII patterns with a marker."""
    out = text
    for s in secrets:
        if s:
            out = out.replace(s, _REDACTED)
    for pat in _PATTERNS:
        out = pat.sub(_REDACTED, out)
    return out


def redact_obj(obj: JSONValue, secrets: Iterable[str] = ()) -> JSONValue:
    """Recursively redact strings inside a JSON-serializable structure."""
    secrets = list(secrets)
    if isinstance(obj, str):
        return redact(obj, secrets)
    if isinstance(obj, dict):
        return {k: redact_obj(v, secrets) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact_obj(v, secrets) for v in obj]
    return obj


class ScrubbingStore:
    """A ``Store`` wrapper that redacts secrets/PII before any write.

    Wrap a backing Store so transcripts, outputs, and telemetry are redacted on the
    way in — the persisted ledger never contains a raw credential.
    """

    def __init__(self, inner: Store, secrets: Iterable[str] = ()) -> None:
        self._inner = inner
        self._secrets = list(secrets)

    def _redact_dict(self, data: dict[str, JSONValue]) -> dict[str, JSONValue]:
        return {k: redact_obj(v, self._secrets) for k, v in data.items()}

    def put_record(
        self, kind: str, id: str, data: dict[str, JSONValue], *, org_id: str = "local"
    ) -> None:
        self._inner.put_record(kind, id, self._redact_dict(data), org_id=org_id)

    def get_record(
        self, kind: str, id: str, *, org_id: str = "local"
    ) -> dict[str, JSONValue] | None:
        return self._inner.get_record(kind, id, org_id=org_id)

    def list_records(self, kind: str, *, org_id: str = "local") -> list[dict[str, JSONValue]]:
        return self._inner.list_records(kind, org_id=org_id)

    def delete_record(self, kind: str, id: str, *, org_id: str = "local") -> None:
        self._inner.delete_record(kind, id, org_id=org_id)

    def kv_get(self, namespace: str, key: str, *, org_id: str = "local") -> JSONValue | None:
        return self._inner.kv_get(namespace, key, org_id=org_id)

    def kv_set(self, namespace: str, key: str, value: JSONValue, *, org_id: str = "local") -> None:
        self._inner.kv_set(namespace, key, redact_obj(value, self._secrets), org_id=org_id)

    def claim_idempotency(self, key: str, *, org_id: str = "local") -> bool:
        return self._inner.claim_idempotency(key, org_id=org_id)

    def append_event(
        self, run_id: str, event: dict[str, JSONValue], *, org_id: str = "local"
    ) -> None:
        self._inner.append_event(run_id, self._redact_dict(event), org_id=org_id)

    def events(self, run_id: str, *, org_id: str = "local") -> list[dict[str, JSONValue]]:
        return self._inner.events(run_id, org_id=org_id)

    def close(self) -> None:
        self._inner.close()


class Capabilities:
    """What a package/unit declares it needs (the consent surface)."""

    def __init__(
        self, *, secrets: list[str] | None = None, egress: list[str] | None = None
    ) -> None:
        self.secrets = secrets or []
        self.egress = egress or []

    def summary(self) -> str:
        parts = []
        if self.secrets:
            parts.append(f"secrets: {', '.join(self.secrets)}")
        if self.egress:
            parts.append(f"network egress: {', '.join(self.egress)}")
        return "; ".join(parts) if parts else "no special capabilities"


@dataclass(frozen=True)
class Grant:
    """A recorded, consented capability grant for an installed package.

    The persisted record that an install-time consent (CRA-180) produces: which
    secrets and egress destinations the user approved for ``package``. The broker
    (CRA-178) and the jail (CRA-179) consume this shape to enforce least privilege;
    CRA-180 owns the grant *manifest* (creation/storage). Frozen + content-stable.
    """

    package: str
    secrets: tuple[str, ...] = ()
    egress: tuple[str, ...] = ()
    granted_at: float = 0.0  # epoch seconds; set at consent time
    grant_id: str = field(default_factory=new_id)

    def permits_secret(self, ref: str) -> bool:
        """True if this grant covers secret reference ``ref``."""
        return ref in self.secrets

    def permits_egress(self, destination: str) -> bool:
        """True if this grant covers network egress to ``destination``."""
        return destination in self.egress


def read_capabilities(project_dir: str | Path) -> Capabilities:
    """Read a package's declared capabilities from ``crawfish.toml [capabilities]``."""
    path = Path(project_dir) / "crawfish.toml"
    if not path.exists():
        return Capabilities()
    data = tomllib.loads(path.read_text()).get("capabilities", {})
    return Capabilities(secrets=list(data.get("secrets", [])), egress=list(data.get("egress", [])))
