"""Web UI: the Hosts page (a table of the SSH config's hosts, each checked as the page loads) and a terminal
page per host (xterm.js over a WebSocket, terminal.py). lo9i starts it (see server.yaml) and shows it in
its web app:

    HOSTS_DB=/path/hosts.db hosts-web --socket /path/web.sock

It listens only on a Unix socket that only lo9i's daemon reaches, so it has no login: the daemon lets only
the user's paired devices through, the terminal's WebSocket too. The daemon serves it under a path
(/hosts/) and says which in X-Forwarded-Prefix; pages link relative to it (<base> in base.html).
"""

import argparse
import contextlib
import hashlib
import os
import re
import socket
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import db, service
from ..herdr import ATTACH, Herdr, HerdrError
from ..service import UnknownHostError
from ..ssh import OpenSsh, terminal_command
from .terminal import Terminal, bridge

HERE = Path(__file__).parent
# What the daemon may send as X-Forwarded-Prefix: path segments, like /hosts.
PREFIX = re.compile(r"^(/[A-Za-z0-9_-]+)*$")
# The terminal's size until the browser says.
_COLS, _ROWS = 120, 40


def asset_url(name: str) -> str:
    """A static file's URL with a hash of its content, so a browser fetches it again after an update
    changes it instead of using its cached copy."""
    digest = hashlib.sha256((HERE / "static" / name).read_bytes()).hexdigest()[:10]
    return f"static/{name}?v={digest}"


def ago(moment: str | None) -> str:
    if not moment:
        return "Never"
    seconds = (datetime.now(UTC) - datetime.fromisoformat(moment)).total_seconds()
    for size, unit in ((86400, "day"), (3600, "hour"), (60, "minute")):
        if seconds >= size:
            n = int(seconds // size)
            return f"{n} {unit}{'' if n == 1 else 's'} ago"
    return "Just now"


templates = Jinja2Templates(directory=HERE / "templates")
templates.env.globals |= {"asset": asset_url}
templates.env.filters["ago"] = ago


def _render(request: Request, template: str, **ctx: Any) -> HTMLResponse:
    return templates.TemplateResponse(request, template, ctx)


def _herdr(request: Request) -> Herdr:
    return request.app.state.herdr


def _hosts_routes(app: FastAPI) -> None:
    @app.get("/", response_class=HTMLResponse)
    async def hosts(request: Request, hidden: bool = False) -> HTMLResponse:
        every = await service.all_hosts()
        shown = every if hidden else [h for h in every if not h.hidden]
        hidden_count = sum(1 for h in every if h.hidden)
        return _render(request, "hosts.html", hosts=shown, hidden_count=hidden_count, showing_hidden=hidden)

    @app.get("/hosts/{name}/row", response_class=HTMLResponse)
    async def row(request: Request, name: str) -> HTMLResponse:
        """A host's row with what checking it found: whether it answers, and its Herdr session."""
        host = await service.host(name)
        probe = await _herdr(request).probe(name)
        return _render(request, "_row.html", h=host, probe=probe)

    @app.post("/hosts/{name}/hide")
    async def hide(name: str) -> Response:
        await service.set_hidden(name, True)
        return Response(headers={"HX-Refresh": "true"})

    @app.post("/hosts/{name}/show")
    async def show(name: str) -> Response:
        await service.set_hidden(name, False)
        return Response(headers={"HX-Refresh": "true"})


def _terminal_routes(app: FastAPI, command: Callable[[str], list[str]]) -> None:
    @app.get("/hosts/{name}", response_class=HTMLResponse)
    async def terminal_page(request: Request, name: str) -> HTMLResponse:
        return _render(request, "terminal.html", h=await service.host(name))

    @app.websocket("/hosts/{name}/install")
    async def install_herdr(websocket: WebSocket, name: str) -> None:
        """Installs Herdr on the host, from the terminal page's button. A WebSocket, since a download can
        take longer than lo9i waits for a request; it answers {"ok", "message"} once done."""
        if not await _known(websocket, name):
            return
        await websocket.accept()
        try:
            await websocket.app.state.herdr.install(name)
            answer = {"ok": True, "message": f"Herdr is installed on {name}."}
        except HerdrError as e:
            answer = {"ok": False, "message": str(e)}
        await websocket.send_json(answer)
        await websocket.close()

    @app.websocket("/hosts/{name}/terminal")
    async def terminal(websocket: WebSocket, name: str, cols: int = _COLS, rows: int = _ROWS) -> None:
        if not await _known(websocket, name):
            return
        await websocket.accept()
        try:
            attached = await Terminal.start(command(name), cols, rows)
        except OSError as e:
            message = f"This computer couldn't start ssh: {e.strerror or e}."
            await websocket.send_json({"type": "ended", "reason": "failed", "message": message})
            await websocket.close()
            return
        await bridge(websocket, attached, name, lambda: service.connected(name))


async def _known(websocket: WebSocket, name: str) -> bool:
    """Whether the config has the host; if not, the connection is refused."""
    try:
        await service.host(name)
    except UnknownHostError:
        await websocket.close(1008, f"No host named {name}.")
        return False
    return True


class ForwardedPrefix:
    """Takes the path the daemon serves this under from X-Forwarded-Prefix (the daemon strips it from the
    request path), so pages can link to it. WebSockets come with it too."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] in ("http", "websocket"):
            prefix = dict(scope["headers"]).get(b"x-forwarded-prefix", b"").decode("latin-1")
            if prefix and PREFIX.match(prefix):
                # ASGI's path includes root_path; static files (and url_for) count on it.
                scope = {
                    **scope,
                    "root_path": prefix,
                    "path": prefix + scope["path"],
                    "raw_path": prefix.encode() + scope.get("raw_path", scope["path"].encode()),
                }
        await self.app(scope, receive, send)


def attach_command(name: str) -> list[str]:
    """ssh to the host, attached to lo9i's Herdr session there."""
    return terminal_command(name, ATTACH)


def create_app(herdr: Herdr | None = None, command: Callable[[str], list[str]] = attach_command) -> FastAPI:
    """Tests pass a Herdr over a fake ssh, and a local command for the terminal."""
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.herdr = herdr or Herdr(OpenSsh())

    @app.exception_handler(UnknownHostError)
    async def unknown_host(request: Request, exc: UnknownHostError) -> Response:
        return Response(str(exc), status_code=404, media_type="text/plain")

    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    app.add_middleware(ForwardedPrefix)
    _hosts_routes(app)
    _terminal_routes(app, command)
    return app


def main() -> None:
    parser = argparse.ArgumentParser(prog="hosts-web")
    parser.add_argument("--socket", required=True, help="Unix socket to listen on")
    args = parser.parse_args()
    db.connect().close()

    import uvicorn

    sock = listen(args.socket)  # Kept referenced: closing it would close the fd uvicorn serves.
    uvicorn.run(create_app(), fd=sock.fileno())


def listen(path: str) -> socket.socket:
    """A Unix socket only this user can connect to. The page has no login, so other users on the computer
    must not reach it (uvicorn's own `uds` makes the socket world-writable)."""
    with contextlib.suppress(FileNotFoundError):
        os.unlink(path)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    old = os.umask(0o177)
    try:
        sock.bind(path)
    finally:
        os.umask(old)
    sock.listen(128)
    return sock
