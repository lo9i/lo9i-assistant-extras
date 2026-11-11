"""The plugin's page in lo9i's web app (Browser in its sidebar): the browser seen live, on a computer without a
screen, so the user signs in to sites there (live.py). lo9i starts it (see server.yaml):

    BROWSER_DATA=/path/data browser-web --socket /path/web.sock

It listens only on a Unix socket that only lo9i's daemon reaches, so it has no login: the daemon lets only the
user's paired devices through, its WebSocket too. The daemon serves it under a path (/browser/) and says which
in X-Forwarded-Prefix; the page links relative to it (<base>).

Its WebSocket (/live) is passed through to the MCP server's live view while the browser shows, line by line;
when it doesn't (the server isn't running, or the browser isn't shown), the page is told `off` and tries again
every few seconds, so it shows the browser as soon as the assistant shows it.
"""

import argparse
import asyncio
import contextlib
import hashlib
import json
import os
import re
import socket
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocket, WebSocketDisconnect

from lo9i_browser import places

HERE = Path(__file__).parent
STATIC = HERE / "static"
# What the daemon may send as X-Forwarded-Prefix: path segments, like /browser.
PREFIX = re.compile(r"^(/[A-Za-z0-9_-]+)*$")
_LINE_LIMIT = 16 * 1024 * 1024


def asset_url(name: str) -> str:
    """A static file's URL with a hash of its content, so a browser fetches it again after an update."""
    digest = hashlib.sha256((STATIC / name).read_bytes()).hexdigest()[:10]
    return f"static/{name}?v={digest}"


def _prefix(headers: Any) -> str:
    prefix = headers.get("x-forwarded-prefix", "")
    return prefix if PREFIX.match(prefix) else ""


def create_app(socket_path: Path | None = None, screen: bool | None = None) -> Starlette:
    """`socket_path` is the MCP server's live view (live.py); `screen` whether this computer shows the browser's
    window itself, both found here when not given."""
    live_at = socket_path or places.live_socket(places.data())
    windowed = places.has_screen() if screen is None else screen
    template = (STATIC / "index.html").read_text()

    async def index(request: Request) -> HTMLResponse:
        page = (
            template.replace("{base}", f"{_prefix(request.headers)}/")
            .replace("{screen}", "true" if windowed else "false")
            .replace("{theme.js}", asset_url("theme.js"))
            .replace("{live.css}", asset_url("live.css"))
            .replace("{live.js}", asset_url("live.js"))
        )
        return HTMLResponse(page, headers={"cache-control": "no-store"})

    async def live(websocket: WebSocket) -> None:
        await websocket.accept()
        try:
            reader, writer = await asyncio.open_unix_connection(str(live_at), limit=_LINE_LIMIT)
        except OSError:
            await websocket.send_text(json.dumps({"type": "off"}))
            await websocket.close()
            return
        try:
            await _relay(websocket, reader, writer)
        finally:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()

    return Starlette(
        routes=[
            Route("/", index),
            WebSocketRoute("/live", live),
            Mount("/static", StaticFiles(directory=STATIC), name="static"),
        ]
    )


async def _relay(websocket: WebSocket, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    async def to_browser() -> None:
        while line := await reader.readline():
            await websocket.send_text(line.decode().rstrip("\n"))
        with contextlib.suppress(RuntimeError, WebSocketDisconnect):
            await websocket.close()

    async def to_live() -> None:
        with contextlib.suppress(WebSocketDisconnect):
            while True:
                text = await websocket.receive_text()
                writer.write(text.replace("\n", " ").encode() + b"\n")
                await writer.drain()

    tasks = [asyncio.create_task(to_browser()), asyncio.create_task(to_live())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def main() -> None:
    parser = argparse.ArgumentParser(prog="browser-web")
    parser.add_argument("--socket", required=True, help="Unix socket to listen on")
    args = parser.parse_args()

    import uvicorn

    sock = listen(args.socket)  # Kept referenced: closing it would close the fd uvicorn serves.
    uvicorn.run(create_app(), fd=sock.fileno(), log_level="warning")


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


if __name__ == "__main__":
    main()
