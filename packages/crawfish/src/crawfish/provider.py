"""The unified model/provider contract + the single shared model resolver.

``AgentRuntime`` is the one model-touchpoint seam, but model *resolution* (an agent's
``model`` field → a concrete model id) was duplicated in ``CommandRuntime._resolve_model``
and ``cost._resolve_model``, and there is no normalized **provider** interface — so adding
the Anthropic API / OpenAI / Gemini / local backends (#3, #13) would each reinvent
observability + cost capture, and the cost preview could silently drift from what the
runtime actually runs.

CRA-184 freezes three contracts; their behavioural halves land in #3 (CRA-173), the
configurable-default work (CRA-192), and the router (CRA-182):

* :func:`resolve_model` — the **single** resolver. ``runtime``, ``cost.py``, and the
  router all call this, killing the duplicate logic. Implemented here (behaviour-identical
  to the two former copies) so it can be shared immediately without churn.
* :class:`Provider` — the normalized provider protocol #3 implements.
* :class:`ModelsConfig` / :class:`ProviderPolicy` — named-alias + default-model config
  (CRA-192) and the allowed-provider capability that gates failover (CRA-173) and is
  consented at install (CRA-180).

Model-agnostic by type (ADR 0005): no vendor default is hardcoded *here*; callers pass
their own ``default`` (``CommandRuntime`` owns ``DEFAULT_MODEL``; CRA-192 moves it to config).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from crawfish.core.context import RunContext
    from crawfish.runtime.base import RunRequest, RunResult

__all__ = [
    "ProviderPolicy",
    "ModelsConfig",
    "resolve_model",
    "Provider",
]


class ProviderPolicy(BaseModel):
    """Which providers a Definition is permitted to use. Frozen.

    ``allowed=None`` means any provider is permitted (the local-first default).
    A tuple restricts failover/routing to the listed providers — a data-residency
    decision gated here (CRA-173) and consented at install (CRA-180).
    """

    allowed: tuple[str, ...] | None = None

    model_config = {"frozen": True}

    def permits(self, provider: str) -> bool:
        """True if ``provider`` is allowed under this policy."""
        return self.allowed is None or provider in self.allowed


class ModelsConfig(BaseModel):
    """Project-level model configuration: a default + named aliases. Frozen.

    ``default`` is the fallback for unpinned agents (decouples the hardcoded
    ``DEFAULT_MODEL`` from the runtime — CRA-192). ``aliases`` maps friendly names
    (e.g. ``"fast"``) to concrete model ids, resolved by :func:`resolve_model`.
    """

    default: str | None = None
    aliases: dict[str, str] = Field(default_factory=dict)
    policy: ProviderPolicy = Field(default_factory=ProviderPolicy)

    model_config = {"frozen": True}


def resolve_model(
    model: str | list[str] | None,
    *,
    default: str,
    config: ModelsConfig | None = None,
) -> str:
    """Resolve an agent's ``model`` field to a single concrete model id.

    The **one** canonical resolver (former duplicates in ``CommandRuntime`` and
    ``cost.py`` delegate here):

    * ``None`` (unpinned) → ``config.default`` if set, else ``default``;
    * ``str`` → itself, after alias expansion via ``config.aliases``;
    * ``list`` → its first entry (the primary; failover order), alias-expanded;
      an empty list falls back like ``None``.

    Alias expansion is a single hop (an alias must map to a concrete id, not another
    alias) and is deterministic.
    """
    aliases: Mapping[str, str] = config.aliases if config is not None else {}
    fallback = config.default if (config is not None and config.default) else default

    if model is None:
        chosen = fallback
    elif isinstance(model, str):
        chosen = model
    elif model:  # non-empty list
        chosen = model[0]
    else:  # empty list
        chosen = fallback

    return aliases.get(chosen, chosen)


@runtime_checkable
class Provider(Protocol):
    """A normalized model backend behind :class:`~crawfish.runtime.base.AgentRuntime`.

    Implementations (Anthropic API / OpenAI / Gemini / local — #3, #13) expose a uniform
    surface so observability + cost capture are written once. The protocol is structural:
    any object with these members satisfies it.
    """

    name: str

    def models(self) -> list[str]:
        """The concrete model ids this provider can serve."""
        ...

    def supports(self, model: str) -> bool:
        """True if this provider can serve ``model``."""
        ...

    async def run(self, request: RunRequest, ctx: RunContext) -> RunResult:
        """Execute one model turn and return the normalized result."""
        ...
