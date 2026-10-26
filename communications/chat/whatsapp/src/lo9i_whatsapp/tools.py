"""How the plugin's tools (mcp_server.py, a process of their own) reach the WhatsApp connection, which only the
channel's process holds: WhatsApp allows one connection per linked device. The channel listens on a Unix socket in
the plugin's data folder that only this user can open, and answers one request per connection, a line of JSON each
way:

    {"op": "send", "to": "+5491112345678", "text": "..."}   →   {"ok": true, "result": "+5491112345678"}
    {"op": "find", "query": "ana"}                          →   {"ok": true, "result": [{"name", "phone"}, ...]}

A request that fails answers {"ok": false, "error": "why"}."""

import asyncio
import contextlib
import json
import logging
import os
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    # Not at run time: the tools' process asks without neonize, which is slow to import.
    from lo9i_whatsapp.others import Others

logger = logging.getLogger(__name__)

SOCKET = "tools.sock"
_TIMEOUT = 60.0
_NOT_RUNNING = "WhatsApp isn't running: the WhatsApp plugin is off, or starting. Try again in a moment."


class ToolError(Exception):
    """The request wasn't carried out; the text says why, for the assistant."""


async def serve(folder: Path, others: "Others") -> None:
    """Answers the tools' requests until cancelled."""
    path = folder / SOCKET
    with contextlib.suppress(FileNotFoundError):
        path.unlink()
    old = os.umask(0o177)
    try:
        server = await asyncio.start_unix_server(lambda r, w: _answer(r, w, others), str(path))
    finally:
        os.umask(old)
    async with server:
        await server.serve_forever()


async def ask(folder: Path, request: dict[str, Any]) -> Any:
    """The channel's answer to `request`. Raises ToolError when it fails or the channel isn't running."""
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_unix_connection(str(folder / SOCKET)), _TIMEOUT)
    except (OSError, TimeoutError) as e:
        raise ToolError(_NOT_RUNNING) from e
    try:
        writer.write(json.dumps(request).encode() + b"\n")
        await writer.drain()
        answer = json.loads(await asyncio.wait_for(reader.readline(), _TIMEOUT) or b"{}")
    except (OSError, TimeoutError, ValueError) as e:
        raise ToolError(f"WhatsApp didn't answer: {e or type(e).__name__}") from e
    finally:
        writer.close()
    if not answer.get("ok"):
        raise ToolError(str(answer.get("error") or _NOT_RUNNING))
    return answer.get("result")


async def _answer(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, others: "Others") -> None:
    try:
        request = json.loads(await reader.readline())
        answer = {"ok": True, "result": await _carry_out(others, request)}
    except ToolError as e:
        answer = {"ok": False, "error": str(e)}
    except (ValueError, KeyError, TypeError) as e:
        answer = {"ok": False, "error": f"A request the channel can't read: {e}"}
    except Exception as e:
        logger.exception("A tool's request failed")
        answer = {"ok": False, "error": f"WhatsApp didn't take it: {e}"}
    writer.write(json.dumps(answer).encode() + b"\n")
    with contextlib.suppress(OSError):
        await writer.drain()
    writer.close()


async def _carry_out(others: "Others", request: dict[str, Any]) -> Any:
    if request["op"] == "send":
        return await others.send(str(request["to"]), str(request["text"]))
    if request["op"] == "find":
        return [asdict(found) for found in await others.find(str(request["query"]))]
    raise ValueError(f"unknown op {request['op']!r}")
