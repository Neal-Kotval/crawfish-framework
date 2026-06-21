"""Profile → runtime selection.

``dev`` → CommandRuntime, ``prod`` → ManagedRuntime; switching profile is a runtime
swap with no code change. Per-agent model overrides are honoured inside each runtime.
"""

from __future__ import annotations

from crawfish.config import ProfileConfig
from crawfish.provider import ModelsConfig
from crawfish.runtime.base import AgentRuntime
from crawfish.runtime.command import CommandRuntime
from crawfish.runtime.mock import MockRuntime
from crawfish.runtime.stubs import ClientRuntime, ManagedRuntime

__all__ = ["get_runtime", "RUNTIME_FACTORIES"]

RUNTIME_FACTORIES: dict[str, type[AgentRuntime]] = {
    "command": CommandRuntime,
    "mock": MockRuntime,
    "client": ClientRuntime,
    "managed": ManagedRuntime,
}


def get_runtime(profile: ProfileConfig, *, config: ModelsConfig | None = None) -> AgentRuntime:
    """Instantiate the runtime named by a resolved profile.

    ``config`` is the project's :class:`~crawfish.provider.ModelsConfig` (named
    aliases + configured default); it is forwarded to the model-resolving
    :class:`CommandRuntime` so an unpinned agent resolves to ``config.default``
    instead of the built-in ``DEFAULT_MODEL``. Runtimes that don't yet consume it
    are constructed unchanged.
    """
    name = profile.runtime
    factory = RUNTIME_FACTORIES.get(name)
    if factory is None:
        raise KeyError(f"unknown runtime {name!r} (known: {sorted(RUNTIME_FACTORIES)})")
    if config is not None and factory is CommandRuntime:
        return CommandRuntime(config=config)
    return factory()
