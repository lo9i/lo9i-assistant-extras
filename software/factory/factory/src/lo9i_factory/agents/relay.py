"""Claude Code on the user's GitHub Copilot plan (models.implementer set to
copilot:<a Claude model>), the way lo9i's claude-code plugin runs it (its
relay.py and copilot.py, copied: plugins share no code).

Copilot answers Anthropic's Messages API (/v1/messages) for its Claude
models. A relay on this computer, one per build, takes Claude Code's
requests (ANTHROPIC_BASE_URL) with a random key of its own
(ANTHROPIC_AUTH_TOKEN), signs them with a current Copilot token
(providers/copilot.py), adds the headers Copilot needs, uses Copilot's model
ids, says how Copilot should count each request, and streams the answers
back. Claude Code never gets the GitHub token.

It listens on 127.0.0.1 only. In docker mode the container reaches it as
host.docker.internal, which Docker Desktop forwards to the computer's
loopback; Docker Engine on Linux doesn't, so there the agent must run local.
"""

import asyncio
import contextlib
import json
import re
import secrets
import socket
from collections.abc import AsyncIterator

import httpx2
import uvicorn
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from ..providers import ProviderError
from ..providers.copilot import HEADERS, CopilotToken, CopilotTokens

# Headers of Claude Code's requests that Copilot gets too; its key and the rest stay here.
_PASSED = ("content-type", "accept", "anthropic-version", "anthropic-beta")
# Answers can stream for minutes; connecting shouldn't take long.
_UPSTREAM_TIMEOUT = httpx2.Timeout(600, connect=30)
_MODELS_TIMEOUT = 20
# What Claude Code asks for by family when it names no model Copilot has.
FAMILIES = ("opus", "sonnet", "haiku")
# The computer, as a container of Docker Desktop's sees it.
DOCKER_HOST = "host.docker.internal"


class Relay:
    def __init__(self, github_token: str, transport: httpx2.AsyncBaseTransport | None = None) -> None:
        """`transport` stands in for GitHub and Copilot in tests."""
        self._github_token = github_token
        self._transport = transport
        self.key = secrets.token_urlsafe(32)
        # Copilot's Claude models on /v1/messages.
        self.models: list[str] = []
        self.port = 0
        # The premium requests Copilot counts (X-Initiator: user).
        self.requests = 0

    def model(self, wanted: str) -> str:
        """Copilot's id for the implementer's model. Raises ProviderError when Copilot has none such."""
        found = copilot_id(wanted, self.models)
        if not found:
            has = ", ".join(self.models) or "none"
            raise ProviderError(f"GitHub Copilot has no Claude model {wanted} for Claude Code; it has {has}")
        return found

    def claude_env(self, model: str, docker: bool) -> dict[str, str]:
        """How Claude Code reaches the relay, and Copilot's newest model of each family for what it asks
        on its own (a quick model for small jobs)."""
        newest = {f: _newest(f, self.models) for f in FAMILIES}
        env = {
            "ANTHROPIC_BASE_URL": f"http://{DOCKER_HOST if docker else '127.0.0.1'}:{self.port}",
            "ANTHROPIC_AUTH_TOKEN": self.key,
            "ANTHROPIC_MODEL": model,
            # No update checks, error reports or telemetry: only model calls go out, through Copilot.
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        }
        return env | {f"ANTHROPIC_DEFAULT_{f.upper()}_MODEL": m for f, m in newest.items() if m}

    @contextlib.asynccontextmanager
    async def running(self) -> AsyncIterator["Relay"]:
        """Serves until the block ends. Raises ProviderError when GitHub gives no Copilot token, or
        Copilot doesn't list its models."""
        async with httpx2.AsyncClient(timeout=_UPSTREAM_TIMEOUT, transport=self._transport) as http:
            self._http = http
            self._tokens = CopilotTokens(self._github_token, http)
            self.models = await _claude_models(http, await self._tokens.current())
            async with self._serving():
                yield self

    @contextlib.asynccontextmanager
    async def _serving(self) -> AsyncIterator[None]:
        """The socket listens before uvicorn starts, so a request sent meanwhile waits in its queue."""
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        self.port = listener.getsockname()[1]
        app = Starlette(routes=[Route("/{path:path}", self._relay, methods=["GET", "POST"])])
        server = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="off"))
        task = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            yield
        finally:
            server.should_exit = True
            await task

    async def _relay(self, request: Request) -> Response:
        if not secrets.compare_digest(_key_of(request), self.key):
            return JSONResponse({"error": "unknown key"}, status_code=401)
        body, counted_as = rewrite(await request.body(), self.models)
        upstream = await self._send(request, body, counted_as)
        if counted_as == "user":
            self.requests += 1
        headers = {k: v for k, v in upstream.headers.items() if k.lower() in ("content-type", "request-id")}
        return StreamingResponse(
            upstream.aiter_bytes(), upstream.status_code, headers, background=BackgroundTask(upstream.aclose)
        )

    async def _send(self, request: Request, body: bytes, counted_as: str) -> httpx2.Response:
        """Once more with a new token when Copilot rejects the current one."""
        token = await self._tokens.current()
        upstream = await self._http.send(self._request(request, body, counted_as, token), stream=True)
        if upstream.status_code != 401:
            return upstream
        await upstream.aclose()
        self._tokens.discard(token)
        retried = self._request(request, body, counted_as, await self._tokens.current())
        return await self._http.send(retried, stream=True)

    def _request(self, request: Request, body: bytes, counted_as: str, token: CopilotToken) -> httpx2.Request:
        passed = {k: v for k, v in request.headers.items() if k.lower() in _PASSED}
        headers = {
            **passed,
            **HEADERS,
            "Authorization": f"Bearer {token.value}",
            "X-Initiator": counted_as,
            "Accept-Encoding": "identity",
        }
        url = f"{token.api}/{request.path_params['path']}"
        return self._http.build_request(request.method, url, params=request.query_params, headers=headers, content=body)


def initiator(body: dict) -> str:
    """How Copilot counts the request: `user` when it answers the user's message (a premium request),
    `agent` when it continues after tool results, as VS Code and lo9i report it. Claude Code may end
    the messages with a system one (its reminders): the last of the others decides."""
    messages = [m for m in body.get("messages") or [] if isinstance(m, dict) and m.get("role") != "system"]
    last = messages[-1] if messages else {}
    content = last.get("content")
    results = isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)
    return "user" if last.get("role") == "user" and not results else "agent"


def copilot_id(requested: str, available: list[str]) -> str:
    """Copilot's id for a model, "" when it has none: its own ids use dots ("claude-opus-5.5") where
    Anthropic's use dashes and dates ("claude-opus-5-5-20260101")."""
    if requested in available:
        return requested
    dotted = re.sub(r"-(\d+)-(\d+)$", r"-\1.\2", re.sub(r"-\d{8}$", "", requested))
    return dotted if dotted in available else ""


def model_for(requested: str, available: list[str]) -> str:
    """The model Copilot answers a request for `requested` with: the same, or its newest of the
    family; one it can't place goes as it is."""
    family = next((f for f in FAMILIES if f in requested), "")
    return copilot_id(requested, available) or (family and _newest(family, available)) or requested


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


async def _claude_models(http: httpx2.AsyncClient, token: CopilotToken) -> list[str]:
    """The Claude models Copilot answers on /v1/messages."""
    try:
        response = await http.get(
            f"{token.api}/models",
            headers={"Authorization": f"Bearer {token.value}", **HEADERS},
            timeout=_MODELS_TIMEOUT,
        )
        response.raise_for_status()
    except httpx2.HTTPError as e:
        raise ProviderError(f"couldn't read GitHub Copilot's models: {e}") from e
    return [
        m["id"]
        for m in response.json().get("data", [])
        if "claude" in m["id"] and "/v1/messages" in (m.get("supported_endpoints") or [])
    ]


def _newest(family: str, available: list[str]) -> str:
    """Copilot's newest model of a family, like claude-opus-5.5; "" when it has none."""
    same = sorted((m for m in available if family in m), key=_version, reverse=True)
    return same[0] if same else ""


def _version(model: str) -> tuple[float, ...]:
    return tuple(float(n) for n in re.findall(r"\d+(?:\.\d+)?", model))


def _key_of(request: Request) -> str:
    """Claude Code sends ANTHROPIC_AUTH_TOKEN as a bearer token."""
    bearer = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    return bearer or request.headers.get("x-api-key", "")
