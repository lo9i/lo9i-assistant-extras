"""The user's GitHub Copilot plan as Claude Code's model: Copilot answers the Anthropic Messages API
(`/v1/messages`) for its Claude models. lo9i shares the GitHub token of its Copilot model provider
(`model_login: copilot` in server.yaml, lo9i's core/mcp/logins.py), which is exchanged at GitHub for a
Copilot token lasting about 30 minutes. The exchange and the headers follow lo9i's core/llm/copilot.py;
a plugin can't import lo9i's code, so they're repeated here.
"""

import asyncio
import json
import re
import time
from dataclasses import dataclass

import httpx

EXCHANGE_URL = "https://api.github.com/copilot_internal/v2/token"
DEFAULT_API = "https://api.githubcopilot.com"
# Copilot accepts requests from known editors; these match VS Code's Copilot Chat, as lo9i's do.
HEADERS = {
    "Editor-Version": "vscode/1.104.1",
    "Copilot-Integration-Id": "vscode-chat",
    "Openai-Intent": "conversation-edits",
}
_EXCHANGE_USER_AGENT = "GitHubCopilotChat/0.26.7"
# A token this close to expiring is replaced before use.
_REFRESH_MARGIN_SECONDS = 120
_TIMEOUT = 20
# What Claude Code asks for by family when it names no model Copilot has.
FAMILIES = ("opus", "sonnet", "haiku")


class CopilotError(Exception):
    pass


@dataclass(frozen=True)
class CopilotToken:
    value: str
    expires_at: float
    # The account's API host, from the exchange.
    api: str

    def fresh(self, now: float) -> bool:
        return now < self.expires_at - _REFRESH_MARGIN_SECONDS


class CopilotTokens:
    """The current Copilot token, exchanged again shortly before it expires or after a rejection."""

    def __init__(self, github_token: str, http: httpx.AsyncClient) -> None:
        self._github_token = github_token
        self._http = http
        self._token: CopilotToken | None = None
        self._lock = asyncio.Lock()

    async def current(self) -> CopilotToken:
        async with self._lock:
            if self._token is None or not self._token.fresh(time.time()):
                self._token = await self._exchange()
            return self._token

    def discard(self, token: CopilotToken) -> None:
        if self._token == token:
            self._token = None

    async def _exchange(self) -> CopilotToken:
        headers = {
            "Authorization": f"token {self._github_token}",
            "Accept": "application/json",
            "Editor-Version": HEADERS["Editor-Version"],
            "User-Agent": _EXCHANGE_USER_AGENT,
        }
        response = await self._http.get(EXCHANGE_URL, headers=headers, timeout=_TIMEOUT)
        if response.status_code != 200:
            raise CopilotError(f"GitHub didn't give a Copilot token (HTTP {response.status_code}).")
        data = response.json()
        api = str((data.get("endpoints") or {}).get("api") or DEFAULT_API).rstrip("/")
        return CopilotToken(str(data["token"]), float(data.get("expires_at") or time.time() + 1800), api)


def initiator(body: dict) -> str:
    """How Copilot counts the request: `user` when it answers the user's message (a premium request),
    `agent` when it continues after tool results, as VS Code and lo9i report it."""
    messages = body.get("messages") or [{}]
    last = messages[-1]
    content = last.get("content")
    results = isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)
    return "user" if last.get("role") == "user" and not results else "agent"


def model_for(requested: str, available: list[str]) -> str:
    """Copilot's id for the model Claude Code asks for: its own ids use dots ("claude-opus-5.5") where
    Anthropic's use dashes and dates ("claude-opus-5-5-20260101"). A model Copilot doesn't have becomes
    its newest one of the same family."""
    if requested in available:
        return requested
    dotted = re.sub(r"-(\d+)-(\d+)$", r"-\1.\2", re.sub(r"-\d{8}$", "", requested))
    if dotted in available:
        return dotted
    family = next((f for f in FAMILIES if f in requested), "")
    same = sorted((m for m in available if family and family in m), key=_version, reverse=True)
    return same[0] if same else requested


def newest(family: str, available: list[str]) -> str:
    """Copilot's newest model of a family, like claude-opus-5.5; "" when it has none."""
    same = sorted((m for m in available if family in m), key=_version, reverse=True)
    return same[0] if same else ""


async def claude_models(http: httpx.AsyncClient, token: CopilotToken) -> list[str]:
    """The Claude models Copilot answers on /v1/messages."""
    headers = {"Authorization": f"Bearer {token.value}", **HEADERS}
    response = await http.get(f"{token.api}/models", headers=headers, timeout=_TIMEOUT)
    response.raise_for_status()
    return [
        m["id"]
        for m in response.json().get("data", [])
        if "claude" in m["id"] and "/v1/messages" in (m.get("supported_endpoints") or [])
    ]


def _version(model: str) -> tuple[float, ...]:
    return tuple(float(n) for n in re.findall(r"\d+(?:\.\d+)?", model))


def rewrite(raw: bytes, available: list[str]) -> tuple[bytes, str]:
    """The request with Copilot's model id, and how Copilot counts it. A body that isn't JSON passes
    as it is, counted as `agent` (it's no message from the user)."""
    try:
        body = json.loads(raw)
    except ValueError:
        return raw, "agent"
    if not isinstance(body, dict):
        return raw, "agent"
    if isinstance(body.get("model"), str):
        body["model"] = model_for(body["model"], available)
    return json.dumps(body).encode(), initiator(body)
