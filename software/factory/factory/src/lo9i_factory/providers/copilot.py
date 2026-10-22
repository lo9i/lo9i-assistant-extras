"""GitHub Copilot, with the GitHub token of lo9i's own Copilot sign-in
(LO9I_COPILOT_GITHUB_TOKEN, server.yaml's model_login), the way lo9i's core
talks to it (core/llm/copilot.py, copied: plugins share no code).

Copilot doesn't take the GitHub token directly. It's exchanged at GitHub for
a Copilot token that lasts about 30 minutes and names the account's API host;
requests go to that host, on the API it serves each model on. Copilot counts
a premium request for each one a user starts (X-Initiator: user), which each
of the team's calls is: the meter counts them as requests, not USD.
"""

import asyncio
import time
from collections.abc import AsyncGenerator
from dataclasses import dataclass

import httpx2
from openai import AsyncOpenAI

from . import ProviderError, Reply, Request
from .anthropic_api import AnthropicProvider
from .openai_api import ChatProvider, ResponsesProvider

_EXCHANGE_URL = "https://api.github.com/copilot_internal/v2/token"
DEFAULT_API = "https://api.githubcopilot.com"
# Copilot accepts requests from known editors; these match VS Code's Copilot Chat.
_EDITOR_VERSION = "vscode/1.104.1"
_EXCHANGE_USER_AGENT = "GitHubCopilotChat/0.26.7"
HEADERS = {
    "Editor-Version": _EDITOR_VERSION,
    "Copilot-Integration-Id": "vscode-chat",
    "Openai-Intent": "conversation-edits",
    "X-Initiator": "user",
}
# A token this close to expiring is replaced before use.
_REFRESH_MARGIN_SECONDS = 120
_EXCHANGE_TIMEOUT = 20
# A long proposal takes minutes.
_TIMEOUT = 600


@dataclass(frozen=True)
class CopilotToken:
    value: str
    expires_at: float
    # The account's API host, e.g. https://api.individual.githubcopilot.com.
    api: str

    def fresh(self, now: float) -> bool:
        return now < self.expires_at - _REFRESH_MARGIN_SECONDS


class CopilotTokens:
    """The current Copilot token for a GitHub token, exchanged again when it's
    about to expire. Concurrent calls share one exchange."""

    def __init__(self, github_token: str, http: httpx2.AsyncClient) -> None:
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
        """The API rejected `token`: the next call exchanges a new one."""
        if self._token == token:
            self._token = None

    async def _exchange(self) -> CopilotToken:
        headers = {
            "Authorization": f"token {self._github_token}",
            "Accept": "application/json",
            "Editor-Version": _EDITOR_VERSION,
            "User-Agent": _EXCHANGE_USER_AGENT,
        }
        try:
            response = await self._http.get(_EXCHANGE_URL, headers=headers, timeout=_EXCHANGE_TIMEOUT)
        except httpx2.HTTPError as e:
            raise ProviderError(f"couldn't reach GitHub to get a Copilot token: {e}") from e
        if response.status_code != 200:
            raise ProviderError(_rejection(response))
        return _token(response.json())


class CopilotAuth(httpx2.Auth):
    """Signs each request to the Copilot API and sends it to the account's host."""

    def __init__(self, tokens: CopilotTokens) -> None:
        self._tokens = tokens

    async def async_auth_flow(self, request: httpx2.Request) -> AsyncGenerator[httpx2.Request, httpx2.Response]:
        token = await self._tokens.current()
        response = yield _signed(request, token)
        if response.status_code == 401:
            self._tokens.discard(token)
            yield _signed(request, await self._tokens.current())


class CopilotProvider:
    """Copilot serves each model on the APIs it lists for it (its /models):
    Claude's on Anthropic's Messages API, which keeps to the JSON asked for
    (on chat completions Copilot drops it); GPT's and Grok's on the Responses
    API only; others on chat completions."""

    LABEL = "GitHub Copilot"

    def __init__(self, tokens: CopilotTokens, http: httpx2.AsyncClient) -> None:
        self.tokens = tokens
        self.http = http
        # The api_key is never sent: CopilotAuth sets Authorization on each request.
        openai_client = AsyncOpenAI(api_key="set-per-request", base_url=DEFAULT_API, http_client=http)
        self.chat = ChatProvider(openai_client, self.LABEL)
        self.responses = ResponsesProvider(openai_client, self.LABEL)
        self.messages = AnthropicProvider("set-per-request", http, base_url=DEFAULT_API, label=self.LABEL)
        self._endpoints: dict[str, list[str]] | None = None

    async def complete(self, model: str, request: Request) -> Reply:
        # Exchanged first: an error inside the request would reach us only as
        # the SDK's "connection error", without GitHub's reason.
        await self.tokens.current()
        endpoints = (await self.endpoints()).get(model, [])
        if "/v1/messages" in endpoints:
            return await self.messages.complete(model, request)
        if "/responses" in endpoints and "/chat/completions" not in endpoints:
            return await self.responses.complete(model, request)
        return await self.chat.complete(model, request)

    async def endpoints(self) -> dict[str, list[str]]:
        """The APIs Copilot serves each model on, read once."""
        if self._endpoints is None:
            try:
                response = await self.http.get(f"{DEFAULT_API}/models", timeout=_EXCHANGE_TIMEOUT)
                response.raise_for_status()
            except httpx2.HTTPError as e:
                raise ProviderError(f"couldn't read GitHub Copilot's models: {e}") from e
            self._endpoints = {
                str(m.get("id")): list(m.get("supported_endpoints") or []) for m in response.json().get("data", [])
            }
        return self._endpoints


def copilot_provider(github_token: str, transport: httpx2.AsyncBaseTransport | None = None) -> CopilotProvider:
    """`transport` replaces the network in tests."""
    tokens = CopilotTokens(github_token, httpx2.AsyncClient(transport=transport))
    http = httpx2.AsyncClient(transport=transport, auth=CopilotAuth(tokens), headers=HEADERS, timeout=_TIMEOUT)
    return CopilotProvider(tokens, http)


def _signed(request: httpx2.Request, token: CopilotToken) -> httpx2.Request:
    api = httpx2.URL(token.api)
    request.url = request.url.copy_with(scheme=api.scheme, host=api.host, port=api.port)
    request.headers["Authorization"] = f"Bearer {token.value}"
    return request


def _token(data: dict) -> CopilotToken:
    value = str(data.get("token") or "")
    if not value:
        raise ProviderError("GitHub answered without a Copilot token")
    expires_at = float(data.get("expires_at") or time.time() + 1800)
    endpoints = data.get("endpoints")
    api = str((endpoints if isinstance(endpoints, dict) else {}).get("api") or DEFAULT_API).rstrip("/")
    return CopilotToken(value, expires_at, api)


def _rejection(response: httpx2.Response) -> str:
    if response.status_code == 401:
        return "GitHub rejected lo9i's Copilot sign-in: sign in to GitHub Copilot again in the assistant's Settings"
    if response.status_code in (403, 404):
        return "this GitHub account has no Copilot subscription that allows this"
    return f"GitHub didn't give a Copilot token (HTTP {response.status_code}): {response.text[:200]}"
