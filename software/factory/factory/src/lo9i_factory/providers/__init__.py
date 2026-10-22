"""The models the team runs on: Anthropic and OpenAI through their APIs, and
GitHub Copilot through lo9i's own Copilot sign-in (SPEC §1, §6).

Each provider turns a Request (a system prompt and the turns so far) into a
Reply: the text, the model that answered, and the tokens it used. They only
talk to their API; the meter (meter.py) prices the reply, records it, and
keeps the task within its limit. The clients are async, so the team's
members can be asked at the same time.
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal, Protocol

from ..errors import FactoryError

# The env value each provider's key comes in (server.yaml). Copilot's is
# lo9i's own sign-in (model_login: copilot).
KEYS = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "copilot": "LO9I_COPILOT_GITHUB_TOKEN",
}
# Room for a long proposal; below what the APIs take without streaming.
MAX_TOKENS = 16_000


class ProviderError(FactoryError):
    """A provider couldn't be reached, refused the key, or didn't answer."""


@dataclass(frozen=True)
class Turn:
    role: Literal["user", "assistant"]
    text: str


@dataclass(frozen=True)
class Request:
    system: str
    turns: list[Turn]
    # A JSON schema the reply must follow: the APIs' structured outputs.
    schema: dict | None = None
    max_tokens: int = MAX_TOKENS


@dataclass(frozen=True)
class Usage:
    # Not counting the prompt cache's, which are priced apart.
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


@dataclass(frozen=True)
class Reply:
    text: str
    # The model that answered, as the API names it.
    model: str
    usage: Usage = field(default_factory=Usage)
    # end: finished. max_tokens: cut short. refusal: the model declined.
    stop: Literal["end", "max_tokens", "refusal"] = "end"
    # Why it declined, when the API says.
    detail: str = ""


class Provider(Protocol):
    async def complete(self, model: str, request: Request) -> Reply: ...


def available(env: Mapping[str, str] = os.environ) -> dict[str, bool]:
    """Which providers have a key."""
    return {name: bool(env.get(key)) for name, key in KEYS.items()}


class Providers:
    """A client per provider, made the first time it's used, from the keys in
    `env`. Tests hand it clients of their own."""

    def __init__(self, env: Mapping[str, str] | None = None, clients: dict[str, Provider] | None = None) -> None:
        self._env = os.environ if env is None else env
        self._clients: dict[str, Provider] = dict(clients or {})

    def has(self, name: str) -> bool:
        """Whether this process can reach `name`: a client, or its key."""
        return name in self._clients or bool(self._env.get(KEYS[name]))

    def get(self, name: str) -> Provider:
        if name not in self._clients:
            key = self._env.get(KEYS[name], "")
            if not key:
                raise ProviderError(_missing(name))
            self._clients[name] = _make(name, key)
        return self._clients[name]


def _missing(name: str) -> str:
    if name == "copilot":
        return "GitHub Copilot needs lo9i's Copilot sign-in: choose GitHub Copilot as the assistant's model in Settings"
    return f"{KEYS[name]} isn't set: enter it in the factory plugin's details in Plugins"


def _make(name: str, key: str) -> Provider:
    # Imported here: each SDK loads only when its provider is used.
    if name == "anthropic":
        from .anthropic_api import AnthropicProvider

        return AnthropicProvider(key)
    if name == "openai":
        from .openai_api import openai_provider

        return openai_provider(key)
    from .copilot import copilot_provider

    return copilot_provider(key)
