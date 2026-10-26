"""The WhatsApp page: the QR code that links the user's account, or the account it's linked to. lo9i starts it
(server.yaml) and shows it in its web app:

    WHATSAPP_DATA=/path/data lo9i-whatsapp-page --socket /path/web.sock

The WhatsApp connection is the channel's process, not this one: the page reads what the channel writes in
`$WHATSAPP_DATA/state.json` (link.py), every two seconds while it's open, and asks it for a new QR code by
creating a file there. It listens only on a Unix socket that only lo9i's daemon reaches, so it has no login: the
daemon lets only the user's paired devices through. The daemon serves it under a path (/whatsapp/) and says which
in X-Forwarded-Prefix; links are relative to it (<base> in base.html).
"""

import argparse
import contextlib
import hashlib
import os
import re
import socket
from pathlib import Path
from typing import Any

import segno
from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from lo9i_whatsapp.link import Snapshot, State, ask_for_new_qr, read

HERE = Path(__file__).parent
# What the daemon may send as X-Forwarded-Prefix: path segments, like /whatsapp.
PREFIX = re.compile(r"^(/[A-Za-z0-9_-]+)*$")


def asset_url(name: str) -> str:
    """A static file's URL with a hash of its content, so a browser fetches it again after an update changes it
    instead of using its cached copy."""
    digest = hashlib.sha256((HERE / "static" / name).read_bytes()).hexdigest()[:10]
    return f"static/{name}?v={digest}"


def qr_svg(code: str) -> Markup:
    """Dark on light in either theme, with a quiet border: what phones scan best."""
    svg = segno.make_qr(code).svg_inline(scale=6, border=3, dark="#111", light="#fff", title="WhatsApp QR code")
    return Markup(svg)


def create_app(folder: Path) -> Starlette:
    templates = Environment(loader=FileSystemLoader(HERE / "templates"), autoescape=select_autoescape())
    templates.globals.update(asset=asset_url, qr_svg=qr_svg, State=State)

    def render(name: str, request: Request, link: Snapshot) -> HTMLResponse:
        html = templates.get_template(name).render(request=request, link=link)
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})

    async def home(request: Request) -> HTMLResponse:
        return render("home.html", request, read(folder))

    async def panel(request: Request) -> HTMLResponse:
        return render("_panel.html", request, read(folder))

    async def new_qr(request: Request) -> HTMLResponse:
        if read(folder).state == State.EXPIRED:
            ask_for_new_qr(folder)
        return render("_panel.html", request, read(folder))

    routes = [
        Route("/", home),
        Route("/panel", panel),
        Route("/new-qr", new_qr, methods=["POST"]),
        Mount("/static", StaticFiles(directory=HERE / "static"), name="static"),
    ]
    app = Starlette(routes=routes)
    app.add_middleware(ForwardedPrefix)
    return app


class ForwardedPrefix:
    """Takes the path the daemon serves this under from X-Forwarded-Prefix (the daemon strips it from the request
    path), so pages can link to it."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            prefix = dict(scope["headers"]).get(b"x-forwarded-prefix", b"").decode("latin-1")
            if prefix and PREFIX.match(prefix):
                # ASGI's path includes root_path; static files count on it.
                scope = {
                    **scope,
                    "root_path": prefix,
                    "path": prefix + scope["path"],
                    "raw_path": prefix.encode() + scope.get("raw_path", scope["path"].encode()),
                }
        await self.app(scope, receive, send)


def main() -> None:
    parser = argparse.ArgumentParser(prog="lo9i-whatsapp-page")
    parser.add_argument("--socket", required=True, help="Unix socket to listen on")
    args = parser.parse_args()

    import uvicorn

    sock = listen(args.socket)  # Kept referenced: closing it would close the fd uvicorn serves.
    uvicorn.run(create_app(Path(os.environ["WHATSAPP_DATA"])), fd=sock.fileno())


def listen(path: str) -> socket.socket:
    """A Unix socket only this user can connect to. The page has no login, so other users on the computer must not
    reach it (uvicorn's own `uds` makes the socket world-writable)."""
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
