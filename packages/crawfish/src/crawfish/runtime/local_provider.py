"""LocalHTTPProvider — the cheap local-inference leg of CRA-182 (ADR 0011).

ADR 0011 (the CRA-183 spike) rejects ruvLLM and settles the local-model path as a thin
adapter: a seed-pinned **OpenAI-compatible HTTP** call to a local server (llama.cpp /
``llama-server`` on ``:8080``, or Ollama on ``:11434/v1``) behind the frozen
:class:`~crawfish.provider.Provider` protocol. No heavy dependency, no vendored engine.

It is **local and credential-free** — there is no API key, so it does *not* touch the
deferred cloud-credential path (CRA-178). The single egress point is an *injected*
:data:`LocalTransport` callable; in production it would be a stdlib HTTP POST to
``localhost``, but in **tests a fake transport is injected and no real HTTP/egress
happens** (the determinism + no-live-call rule). ``--seed`` is sent on every request so
a recorded cassette replays bit-for-bit; the provider itself never calls a model.

``name = "local"``, so a :class:`~crawfish.routing.RoutingPolicy` rule (or an agent /
alias) targeting ``model="local"`` routes here through the
:class:`~crawfish.runtime.provider_runtime.ProviderRuntime`.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from crawfish.runtime.base import EventKind, RunRequest, RunResult, RuntimeEvent
from crawfish.runtime.prompt import compile_prompt, pick_agent

if TYPE_CHECKING:
    from crawfish.core.context import RunContext

__all__ = ["LocalHTTPProvider", "LocalTransport", "OpenAIChatRequest"]

# An injected egress callable: given the OpenAI-compatible chat-completions request
# body (already including ``seed``), return the raw JSON response text. The production
# impl is a stdlib POST to localhost; tests inject a fake that returns canned JSON and
# performs no network I/O. Kept narrow so the provider holds no transport policy.
LocalTransport = Callable[["OpenAIChatRequest"], Awaitable[str]]


class OpenAIChatRequest:
    """The OpenAI-compatible ``/v1/chat/completions`` body a local server accepts.

    A plain value object (no pydantic — it is a transport detail, not a public contract):
    ``model``, a single-message ``messages`` list carrying the compiled prompt, and a
    pinned ``seed`` for reproducible decoding. :meth:`as_body` renders the JSON dict the
    transport POSTs; :attr:`endpoint` is the server path (default the de-facto local one).
    """

    def __init__(self, *, model: str, prompt: str, seed: int, endpoint: str) -> None:
        self.model = model
        self.prompt = prompt
        self.seed = seed
        self.endpoint = endpoint

    def as_body(self) -> dict[str, object]:
        return {
            "model": self.model,
            "messages": [{"role": "user", "content": self.prompt}],
            "seed": self.seed,
            "temperature": 0.0,  # greedy + seed = reproducible enough to compare
            "stream": False,
        }


class LocalHTTPProvider:
    """A :class:`~crawfish.provider.Provider` over a local OpenAI-compatible server.

    Satisfies the frozen structural ``Provider`` protocol (``name`` / ``models`` /
    ``supports`` / async ``run``). Credential-free: holds no secret and reads no ``.env``.
    The lone egress is the injected ``transport``; with none injected :meth:`run` raises
    rather than guessing a network call, so it can never silently egress in a test.

    ``cost_usd`` defaults to 0.0 — local inference burns no metered budget, which is the
    whole point of routing cheap steps here.
    """

    def __init__(
        self,
        *,
        name: str = "local",
        models: list[str] | None = None,
        transport: LocalTransport | None = None,
        endpoint: str = "http://localhost:8080/v1/chat/completions",
        seed: int = 0,
        cost_usd: float = 0.0,
    ) -> None:
        self.name = name
        # Default served set includes the ``"local"`` alias the router targets plus the
        # common llama.cpp/Ollama default ids; an explicit list overrides.
        self._models = list(models) if models is not None else ["local"]
        self._transport = transport
        self._endpoint = endpoint
        self._seed = seed
        self._cost_usd = cost_usd

    def models(self) -> list[str]:
        return list(self._models)

    def supports(self, model: str) -> bool:
        return model in self._models

    def _build_request(self, request: RunRequest) -> OpenAIChatRequest:
        agent = pick_agent(request.definition, request.role)
        # compile_prompt enforces the fluid/static prompt-injection boundary: untrusted
        # session data reaches the model only inside the fenced data block.
        prompt = compile_prompt(request.definition, agent, request.inputs)
        model = request.model or (self._models[0] if self._models else self.name)
        return OpenAIChatRequest(
            model=model, prompt=prompt, seed=self._seed, endpoint=self._endpoint
        )

    async def run(self, request: RunRequest, ctx: RunContext) -> RunResult:
        ctx.cancel_token.raise_if_cancelled()
        if self._transport is None:
            raise NotImplementedError(
                f"LocalHTTPProvider {self.name!r} has no injected transport. The local "
                "server call is an injected dependency (a stdlib POST to localhost in "
                "production; a fake in tests) so no real HTTP happens by default."
            )
        chat = self._build_request(request)
        raw = await self._transport(chat)
        text = _parse_chat_completion(raw)
        return RunResult(
            text=text,
            session_id=f"{self.name}-{ctx.run_id}",
            cost_usd=self._cost_usd,
            model=chat.model,
            events=[RuntimeEvent(kind=EventKind.RESULT, text=text, cost_usd=self._cost_usd)],
        )


def _parse_chat_completion(raw: str) -> str:
    """Pull the assistant text out of an OpenAI-compatible chat-completions response.

    Tolerant of shape drift across llama.cpp / Ollama: reads
    ``choices[0].message.content`` and falls back to ``choices[0].text`` (older
    completion shape). A malformed/empty body yields ``""`` rather than raising, so a
    recorded cassette of a degenerate response still replays.
    """
    try:
        obj = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return ""
    choices = obj.get("choices") if isinstance(obj, dict) else None
    if not isinstance(choices, list) or not choices:
        return ""
    first = choices[0]
    if not isinstance(first, dict):
        return ""
    message = first.get("message")
    if isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, str):
            return content
    text = first.get("text")
    return text if isinstance(text, str) else ""
