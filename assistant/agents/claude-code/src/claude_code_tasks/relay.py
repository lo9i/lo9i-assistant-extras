"""A relay on this computer between Claude Code and Copilot, for one task. Claude Code sends its Anthropic
API requests here (ANTHROPIC_BASE_URL); the relay signs them with a current Copilot token, adds the
headers Copilot needs, uses Copilot's model ids, says how Copilot should count each request, and streams
the answers back. Only Claude Code, holding the relay's own random key, can use it."""

import asyncio
import contextlib
import secrets
import socket
from collections.abc import AsyncIterator

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from claude_code_tasks import copilot

# Headers of Claude Code's requests that Copilot gets too; its own key and the rest stay here.
_PASSED = ("content-type", "accept", "anthropic-version", "anthropic-beta")
# Answers can stream for minutes; connecting shouldn't take long.
_UPSTREAM_TIMEOUT = httpx.Timeout(600, connect=30)


class Relay:
    def __init__(self, github_token: str, transport: httpx.AsyncBaseTransport | None = None) -> None:
        """`transport` stands in for GitHub and Copilot in tests."""
        self._github_token = github_token
        self._transport = transport
        self.key = secrets.token_urlsafe(32)
        self.models: list[str] = []
        self.port = 0

    def claude_env(self) -> dict[str, str]:
        """How Claude Code reaches the relay, and Copilot's newest model of each family for it."""
        newest = {f: copilot.newest(f, self.models) for f in copilot.FAMILIES}
        env = {
            "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{self.port}",
            "ANTHROPIC_AUTH_TOKEN": self.key,
            "ANTHROPIC_MODEL": newest["opus"] or newest["sonnet"],
            # No update checks, error reports or telemetry: only model calls go out, through Copilot.
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        }
        env |= {f"ANTHROPIC_DEFAULT_{f.upper()}_MODEL": m for f, m in newest.items() if m}
        return {k: v for k, v in env.items() if v}

    @contextlib.asynccontextmanager
    async def running(self) -> AsyncIterator["Relay"]:
        """Serves until the block ends. Raises CopilotError when GitHub gives no Copilot token."""
        async with httpx.AsyncClient(timeout=_UPSTREAM_TIMEOUT, transport=self._transport) as http:
            self._http = http
            self._tokens = copilot.CopilotTokens(self._github_token, http)
            self.models = await copilot.claude_models(http, await self._tokens.current())
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
        body, counted_as = copilot.rewrite(await request.body(), self.models)
        upstream = await self._send(request, body, counted_as)
        headers = {k: v for k, v in upstream.headers.items() if k.lower() in ("content-type", "request-id")}
        return StreamingResponse(
            upstream.aiter_bytes(), upstream.status_code, headers, background=BackgroundTask(upstream.aclose)
        )

    async def _send(self, request: Request, body: bytes, counted_as: str) -> httpx.Response:
        """Once more with a new token when Copilot rejects the current one."""
        token = await self._tokens.current()
        upstream = await self._http.send(self._request(request, body, counted_as, token), stream=True)
        if upstream.status_code != 401:
            return upstream
        await upstream.aclose()
        self._tokens.discard(token)
        retried = self._request(request, body, counted_as, await self._tokens.current())
        return await self._http.send(retried, stream=True)

    def _request(self, request: Request, body: bytes, counted_as: str, token: copilot.CopilotToken) -> httpx.Request:
        passed = {k: v for k, v in request.headers.items() if k.lower() in _PASSED}
        headers = {
            **passed,
            **copilot.HEADERS,
            "Authorization": f"Bearer {token.value}",
            "X-Initiator": counted_as,
            "Accept-Encoding": "identity",
        }
        url = f"{token.api}/{request.path_params['path']}"
        return self._http.build_request(request.method, url, params=request.query_params, headers=headers, content=body)


def _key_of(request: Request) -> str:
    """Claude Code sends ANTHROPIC_AUTH_TOKEN as a bearer token."""
    bearer = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    return bearer or request.headers.get("x-api-key", "")
