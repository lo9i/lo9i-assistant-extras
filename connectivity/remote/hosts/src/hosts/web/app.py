"""Web UI: the Hosts page (a table of the SSH config's hosts, each checked as the page loads, with its
tunnels, which are switched on and off and added there too) and a terminal page per host (xterm.js over a
WebSocket, terminal.py) with the host's tunnels beside it (tunnels.py), which this process runs, so they
stop with it. lo9i starts it (see server.yaml) and shows it in its web app:

    HOSTS_DB=/path/hosts.db hosts-web --socket /path/web.sock

It listens only on a Unix socket that only lo9i's daemon reaches, so it has no login: the daemon lets only
the user's paired devices through, the terminal's WebSocket too. The daemon serves it under a path
(/hosts/) and says which in X-Forwarded-Prefix; pages link relative to it (<base> in base.html). The
assistant's tools (mcp_server.py) reach the tunnels as JSON under /api/ on the same socket.
"""

import argparse
import asyncio
import contextlib
import hashlib
import os
import re
import socket
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import db, service, tunnels
from ..db import DuplicateTunnelError
from ..herdr import ATTACH, Herdr, HerdrError, Probe
from ..service import Host, UnknownHostError, UnknownTunnelError
from ..ssh import OpenSsh, terminal_command
from ..tunnels import InvalidTunnelError, Status, Tunnel, Tunnels
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


def _tunnels(request: Request) -> Tunnels:
    return request.app.state.tunnels


def _hosts_routes(app: FastAPI) -> None:
    @app.get("/", response_class=HTMLResponse)
    async def hosts(request: Request, hidden: bool = False, host: str = "") -> HTMLResponse:
        """The table at once, each host with what checking it found last, and the host picked (`host`, else the
        first) on the right; the page checks them again over /statuses as soon as it shows."""
        every = await service.all_hosts()
        shown = every if hidden else [h for h in every if not h.hidden]
        hidden_count = sum(1 for h in every if h.hidden)
        tunnels_of = await _tunnels_by_host(request)
        selected = next((h for h in shown if h.name == host), shown[0] if shown else None)
        return _render(
            request, "hosts.html", hosts=shown, hidden_count=hidden_count, showing_hidden=hidden, tunnels_of=tunnels_of,
            probes=request.app.state.probes, selected=selected,
        )  # fmt: skip

    @app.get("/hosts/{name}/details", response_class=HTMLResponse)
    async def details(request: Request, name: str) -> HTMLResponse:
        """The host picked in the table, with what checking it found last."""
        probe = request.app.state.probes.get(name)
        return _render(request, "_details.html", h=await service.host(name), probe=probe)

    @app.websocket("/statuses")
    async def statuses(websocket: WebSocket, hidden: bool = False) -> None:
        """Each shown host's row again as checking it ends, the quickest first. Over one connection: a request
        per row would wait for the browser's few connections per site, behind the hosts that don't answer."""
        await websocket.accept()
        every = await service.all_hosts()
        checks = [asyncio.create_task(_checked(websocket.app, h)) for h in every if hidden or not h.hidden]
        try:
            for check in asyncio.as_completed(checks):
                name, html = await check
                await websocket.send_json({"host": name, "html": html})
            await websocket.close()
        except WebSocketDisconnect:
            pass
        finally:
            for check in checks:
                check.cancel()

    @app.get("/hosts/{name}/row", response_class=HTMLResponse)
    async def row(request: Request, name: str) -> HTMLResponse:
        """A host's row with what checking it found: whether it answers, and its Herdr session."""
        host = await service.host(name)
        probe = await _probe(request.app, name)
        return _render(request, "_row.html", h=host, probe=probe, items=await _items(request, name))

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


def _tunnel_routes(app: FastAPI) -> None:
    """The tunnels (htmx): the terminal page's panel and the hosts table's cell, each with the host's tunnels
    and their switches, and a form to add or change one (in the panel, or in the hosts page's dialog)."""

    @app.get("/hosts/{name}/tunnels", response_class=HTMLResponse)
    async def tunnel_list(request: Request, name: str) -> HTMLResponse:
        return await _tunnel_list(request, name)

    @app.get("/hosts/{name}/tunnels/cell", response_class=HTMLResponse)
    async def tunnel_cell(request: Request, name: str) -> HTMLResponse:
        """The host's tunnels in its row of the hosts table, with their switches."""
        return _render(request, "_tunnel_cell.html", h=await service.host(name), items=await _items(request, name))

    @app.get("/tunnels/new", response_class=HTMLResponse)
    async def new_tunnel_anywhere(request: Request) -> HTMLResponse:
        """The form with the host to pick first: the hosts page's New tunnel."""
        values = {"kind": "local", "target_host": "localhost"}
        return _render(
            request, "_tunnel_form.html", h=None, hosts=await _shown_hosts(), editing=None, values=values, errors={}
        )

    @app.post("/tunnels")
    async def add_tunnel_anywhere(request: Request) -> Response:
        hosts = await _shown_hosts()
        body = (await request.body()).decode(errors="replace")
        picked = (parse_qs(body).get("host") or [""])[-1]
        if picked not in {h.name for h in hosts}:
            values = {key: found[-1] for key, found in parse_qs(body, keep_blank_values=True).items()}
            errors = {"host": "Pick the host it goes through."}
            return _render(
                request, "_tunnel_form.html", h=None, hosts=hosts, editing=None, values=values, errors=errors
            )
        return await _save_tunnel(request, picked, None, hosts)

    @app.get("/hosts/{name}/tunnels/new", response_class=HTMLResponse)
    async def new_tunnel(request: Request, name: str) -> HTMLResponse:
        values = {"kind": "local", "target_host": "localhost"}
        return _render(request, "_tunnel_form.html", h=await service.host(name), editing=None, values=values, errors={})

    @app.get("/hosts/{name}/tunnels/{tunnel_id}/edit", response_class=HTMLResponse)
    async def edit_tunnel(request: Request, name: str, tunnel_id: int) -> HTMLResponse:
        t = await service.tunnel(name, tunnel_id)
        values = {
            "name": t.name, "kind": t.kind, "listen_port": t.listen_port, "target_host": t.target_host or "localhost",
            "target_port": t.target_port or "",
        }  # fmt: skip
        return _render(request, "_tunnel_form.html", h=await service.host(name), editing=t, values=values, errors={})

    @app.post("/hosts/{name}/tunnels")
    async def add_tunnel(request: Request, name: str) -> Response:
        return await _save_tunnel(request, name, None)

    @app.post("/hosts/{name}/tunnels/{tunnel_id}")
    async def change_tunnel(request: Request, name: str, tunnel_id: int) -> Response:
        return await _save_tunnel(request, name, await service.tunnel(name, tunnel_id))

    @app.post("/hosts/{name}/tunnels/{tunnel_id}/start", response_class=HTMLResponse)
    async def start_tunnel(request: Request, name: str, tunnel_id: int) -> HTMLResponse:
        await _tunnels(request).start(await service.tunnel(name, tunnel_id))
        return _changed(await _tunnel_list(request, name))

    @app.post("/hosts/{name}/tunnels/{tunnel_id}/stop", response_class=HTMLResponse)
    async def stop_tunnel(request: Request, name: str, tunnel_id: int) -> HTMLResponse:
        await service.tunnel(name, tunnel_id)
        await _tunnels(request).stop(tunnel_id)
        return _changed(await _tunnel_list(request, name))

    @app.delete("/hosts/{name}/tunnels/{tunnel_id}", response_class=HTMLResponse)
    async def delete_tunnel(request: Request, name: str, tunnel_id: int) -> HTMLResponse:
        await service.tunnel(name, tunnel_id)
        await _tunnels(request).stop(tunnel_id)
        await service.delete_tunnel(tunnel_id)
        return _changed(await _tunnel_list(request, name))


async def _tunnel_list(request: Request, name: str) -> HTMLResponse:
    h = await service.host(name)
    return _render(request, "_tunnels.html", h=h, items=await _items(request, name))


async def _items(request: Request, name: str) -> list[tuple[Tunnel, Status]]:
    return await _items_of(_tunnels(request), name)


async def _items_of(running: Tunnels, name: str) -> list[tuple[Tunnel, Status]]:
    """The host's tunnels, each with how it's doing."""
    return [(t, running.status(t.id)) for t in await service.tunnels(name)]


async def _probe(app: FastAPI, name: str) -> Probe:
    """Checks the host, and keeps what it found for the table's next load (app.state.probes)."""
    probe = await app.state.herdr.probe(name)
    app.state.probes[name] = probe
    return probe


async def _checked(app: FastAPI, host: Host) -> tuple[str, str]:
    """The host's name and its row, checked."""
    probe = await _probe(app, host.name)
    items = await _items_of(app.state.tunnels, host.name)
    return host.name, templates.env.get_template("_row.html").render(h=host, probe=probe, items=items)


async def _tunnels_by_host(request: Request) -> dict[str, list[tuple[Tunnel, Status]]]:
    found: dict[str, list[tuple[Tunnel, Status]]] = {}
    for t in await service.tunnels():
        found.setdefault(t.host, []).append((t, _tunnels(request).status(t.id)))
    return found


def _changed(response: HTMLResponse) -> HTMLResponse:
    """Tells whatever shows the tunnels to read them again: the hosts table's cells switch them without
    taking the answer, which is the terminal page's list."""
    response.headers["HX-Trigger"] = "tunnels-changed"
    return response


async def _save_tunnel(
    request: Request, name: str, editing: Tunnel | None, choosing: list[Host] | None = None
) -> Response:
    """Saves the form's tunnel and has the list show it, or answers the form again with what to change.
    A tunnel that's on runs again as changed. `choosing`: the hosts the form offers, when it has the host
    picked in it (the hosts page's New tunnel)."""
    host = await service.host(name)
    body = (await request.body()).decode(errors="replace")
    values = {key: found[-1] for key, found in parse_qs(body, keep_blank_values=True).items()}
    try:
        typed = tunnels.parse(
            name, values.get("name", ""), values.get("kind", ""), values.get("listen_port"),
            values.get("target_host", ""), values.get("target_port"), editing.id if editing else None,
        )  # fmt: skip
        saved = await service.save_tunnel(typed)
    except InvalidTunnelError as e:
        errors = e.errors
    except DuplicateTunnelError as e:
        errors = {"name": str(e)}
    else:
        await _tunnels(request).restart(saved)
        return Response(headers={"HX-Trigger": "tunnels-changed"})
    shown = None if choosing else host
    return _render(request, "_tunnel_form.html", h=shown, hosts=choosing, editing=editing, values=values, errors=errors)


async def _shown_hosts() -> list[Host]:
    """The hosts the table shows: not the hidden ones."""
    return [h for h in await service.all_hosts() if not h.hidden]


def _api_routes(app: FastAPI) -> None:
    """The tunnels as JSON, for the assistant's tools (mcp_server.py): this process runs them, so the tools
    start and stop them here."""

    @app.get("/api/tunnels")
    async def api_tunnels(request: Request) -> JSONResponse:
        return JSONResponse([described(t, _tunnels(request).status(t.id)) for t in await service.tunnels()])

    @app.post("/api/hosts/{name}/tunnels/{tunnel_id}/start")
    async def api_start(request: Request, name: str, tunnel_id: int) -> JSONResponse:
        t = await service.tunnel(name, tunnel_id)
        return JSONResponse(described(t, await _tunnels(request).start(t)))

    @app.post("/api/hosts/{name}/tunnels/{tunnel_id}/stop")
    async def api_stop(request: Request, name: str, tunnel_id: int) -> JSONResponse:
        t = await service.tunnel(name, tunnel_id)
        await _tunnels(request).stop(tunnel_id)
        return JSONResponse(described(t, _tunnels(request).status(tunnel_id)))


def described(t: Tunnel, status: Status) -> dict[str, Any]:
    """A tunnel and how it's doing, for the assistant."""
    out: dict[str, Any] = {
        "id": t.id, "host": t.host, "name": t.name, "type": t.kind, "listen_port": t.listen_port,
        "description": t.describe(), "state": status.state,
    }  # fmt: skip
    if t.kind != "socks":
        out |= {"target_host": t.target_host, "target_port": t.target_port}
    if status.message:
        out["message"] = status.message
    return out


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


def create_app(
    herdr: Herdr | None = None, command: Callable[[str], list[str]] = attach_command, running: Tunnels | None = None
) -> FastAPI:
    """Tests pass a Herdr over a fake ssh, a local command for the terminal, and tunnels over a fake ssh."""

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # No tunnel outlives the page: lo9i stopping it (or the daemon stopping) stops them all.
        try:
            yield
        finally:
            await app.state.tunnels.stop_all()

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.herdr = herdr or Herdr(OpenSsh())
    app.state.tunnels = running or Tunnels()
    # What checking each host found last, by name: the table shows it at once while it checks them again.
    app.state.probes = {}

    @app.exception_handler(UnknownHostError)
    @app.exception_handler(UnknownTunnelError)
    async def unknown(request: Request, exc: Exception) -> Response:
        return Response(str(exc), status_code=404, media_type="text/plain")

    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    app.add_middleware(ForwardedPrefix)
    _hosts_routes(app)
    _tunnel_routes(app)
    _api_routes(app)
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
