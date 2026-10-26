"""The web page as the assistant's tools reach it: the page runs the tunnels (tunnels.py), so the tools ask it
to add, start, stop and delete them, over the Unix socket it listens on (HOSTS_WEB_SOCKET, which lo9i sets to
it; the page's socket in the database's folder otherwise). One request per connection, answered in JSON.
"""

import asyncio
import json
import os
from pathlib import Path
from typing import Any

from . import db

_TIMEOUT_SECONDS = 15
# Where lo9i has a plugin's page listen, in its data folder (lo9i's core/mcp/folders.py).
_PAGE_SOCKET = "page.sock"


class PageError(Exception):
    """Something the assistant can tell the user, in plain words."""


def socket_path() -> Path:
    chosen = os.environ.get("HOSTS_WEB_SOCKET")
    return Path(chosen) if chosen else db.default_path().parent / _PAGE_SOCKET


async def call(method: str, path: str, body: Any = None) -> Any:
    """The page's JSON answer to a request, with `body` as JSON if given. Raises PageError when it isn't
    running, or for what it answers with an error (a tunnel or host it doesn't know, a tunnel it won't add)."""
    content = b"" if body is None else json.dumps(body).encode()
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_unix_connection(str(socket_path())), _TIMEOUT_SECONDS)
    except (OSError, TimeoutError):
        raise PageError(
            "The Hosts page isn't running, and it's what runs the tunnels. It runs while the hosts plugin is on "
            "in lo9i's Plugins; turning the plugin off and on again starts it again."
        ) from None
    try:
        kind = "Content-Type: application/json\r\n" if body is not None else ""
        writer.write(
            f"{method} {path} HTTP/1.1\r\nHost: hosts\r\n{kind}Content-Length: {len(content)}\r\n"
            f"Connection: close\r\n\r\n".encode()
            + content
        )
        await writer.drain()
        answer = await asyncio.wait_for(reader.read(), _TIMEOUT_SECONDS)
    except (OSError, TimeoutError):
        raise PageError("The Hosts page didn't answer. Try again in a moment.") from None
    finally:
        writer.close()
    head, _, body = answer.partition(b"\r\n\r\n")
    try:
        status = int(head.split(b" ", 2)[1])
    except (IndexError, ValueError):
        raise PageError("The Hosts page didn't answer. Try again in a moment.") from None
    if status >= 400:
        raise PageError(body.decode(errors="replace").strip() or f"The Hosts page answered {status}.")
    return json.loads(body)
