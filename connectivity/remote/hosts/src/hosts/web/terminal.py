"""The web page's terminal: ssh to the host in a pseudo-terminal of its own, attached to lo9i's Herdr
session there (herdr.ATTACH), and passed to the browser's xterm.js over a WebSocket.

On the WebSocket, binary messages are the terminal's bytes, both ways; text messages are JSON: the browser
sends {"type": "resize", "cols", "rows"}, and before closing the page says how the terminal ended,
{"type": "ended", "reason", "message"}, so the browser knows whether to reconnect:

- detached: the user left Herdr (ctrl+b q). It keeps running on the host.
- no-herdr: Herdr isn't installed on the host.
- unreachable: the connection failed or dropped; trying again may work.
- refused: the host doesn't accept the key, or its host key changed; trying again won't help.
- failed: Herdr stopped with an error.

Closing the browser's side stops ssh, which ends Herdr's client there; the session and what runs in it
stay on the host.
"""

import asyncio
import contextlib
import fcntl
import json
import os
import pty
import signal
import struct
import termios
from collections.abc import Awaitable, Callable, Sequence
from functools import partial
from typing import Any

from starlette.websockets import WebSocket, WebSocketDisconnect

from ..herdr import NOT_INSTALLED
from ..ssh import SSH_FAILED, lasting, problem

# The most read from the terminal at once.
_CHUNK = 65536
# After ssh exits, how long what it wrote last may take to be read.
_DRAIN_SECONDS = 2
_STOP_SECONDS = 3


class Terminal:
    """A command in a pseudo-terminal: its output, its input and its size. What it writes to stderr
    (ssh's own messages) is kept apart, to say why it ended."""

    def __init__(self, master: int, process: asyncio.subprocess.Process) -> None:
        self._master = master
        self._process = process
        assert process.stderr is not None  # Started with stderr=PIPE.
        self._errors = asyncio.create_task(process.stderr.read())

    @classmethod
    async def start(cls, command: Sequence[str], cols: int, rows: int, env: dict[str, str] | None = None) -> "Terminal":
        """`env`: what the command's environment gets on top of the page's (connect.py's)."""
        master, child = pty.openpty()
        _set_size(master, cols, rows)
        try:
            process = await asyncio.create_subprocess_exec(
                *command, stdin=child, stdout=child, stderr=asyncio.subprocess.PIPE,
                env=os.environ | {"TERM": "xterm-256color"} | (env or {}), preexec_fn=partial(_take_terminal, child),
            )  # fmt: skip
        except BaseException:
            os.close(master)
            raise
        finally:
            os.close(child)
        os.set_blocking(master, False)
        return cls(master, process)

    async def read(self) -> bytes:
        """What the command wrote; b"" once it's gone."""
        while True:
            try:
                return os.read(self._master, _CHUNK)
            except BlockingIOError:
                await _ready(self._master, reading=True)
            except OSError:  # EIO: no process has the terminal open any more.
                return b""

    async def write(self, data: bytes) -> None:
        while data:
            try:
                data = data[os.write(self._master, data) :]
            except BlockingIOError:
                await _ready(self._master, reading=False)

    def resize(self, cols: int, rows: int) -> None:
        """The kernel tells the command (SIGWINCH), which tells the host."""
        _set_size(self._master, cols, rows)

    async def ended(self) -> tuple[int, str]:
        """Its exit code and what it wrote to stderr, once it exits."""
        code = await self._process.wait()
        err = b"" if self._errors.cancelled() else await self._errors
        return code, err.decode(errors="replace")

    async def stop(self) -> None:
        if self._process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self._process.send_signal(signal.SIGHUP)
            try:
                await asyncio.wait_for(self._process.wait(), _STOP_SECONDS)
            except TimeoutError:
                self._process.kill()
                await self._process.wait()
        self._errors.cancel()
        os.close(self._master)


async def bridge(websocket: WebSocket, terminal: Terminal, host: str, connected: Callable[[], Awaitable[None]]) -> None:
    """Passes the terminal to the browser and back until either one ends. `connected` is called when the
    host first answers (the terminal's first output)."""
    tasks = {
        "output": asyncio.create_task(_output(terminal, websocket, connected)),
        "input": asyncio.create_task(_input(websocket, terminal)),
        "ended": asyncio.create_task(terminal.ended()),
    }
    try:
        await asyncio.wait(tasks.values(), return_when=asyncio.FIRST_COMPLETED)
        if not _browser_left(tasks["input"], tasks["output"]):
            await _finish(websocket, tasks["output"], await tasks["ended"], host)
    finally:
        for task in tasks.values():
            task.cancel()
        await asyncio.gather(*tasks.values(), return_exceptions=True)
        await terminal.stop()


def ending(code: int, err: str, host: str) -> dict[str, Any]:
    """What the page tells the browser about how the terminal ended."""
    if code == 0:
        message = f"You left the Herdr session on {host}. It keeps running there."
        return {"type": "ended", "reason": "detached", "message": message}
    if code == NOT_INSTALLED:
        return {"type": "ended", "reason": "no-herdr", "message": f"Herdr isn't installed on {host}."}
    if code == SSH_FAILED:
        reason = "refused" if lasting(err) else "unreachable"
        return {"type": "ended", "reason": reason, "message": f"Can't reach {host}: {problem(err)}"}
    return {"type": "ended", "reason": "failed", "message": f"Herdr stopped on {host} (exit code {code})."}


def _browser_left(sending: asyncio.Task, showing: asyncio.Task) -> bool:
    """The browser closed its side, or what it was sent couldn't reach it."""
    return sending.done() or (showing.done() and showing.exception() is not None)


async def _finish(websocket: WebSocket, output: asyncio.Task, ended: tuple[int, str], host: str) -> None:
    """The command ended: the browser gets its last output, then how it ended."""
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(asyncio.shield(output), _DRAIN_SECONDS)
    with contextlib.suppress(RuntimeError, WebSocketDisconnect):
        await websocket.send_text(json.dumps(ending(*ended, host)))
        await websocket.close()


async def _output(terminal: Terminal, websocket: WebSocket, connected: Callable[[], Awaitable[None]]) -> None:
    first = True
    while data := await terminal.read():
        await websocket.send_bytes(data)
        if first:
            first = False
            await connected()


async def _input(websocket: WebSocket, terminal: Terminal) -> None:
    while True:
        message = await websocket.receive()
        if message["type"] == "websocket.disconnect":
            return
        if message.get("bytes"):
            await terminal.write(message["bytes"])
        elif message.get("text"):
            _control(terminal, message["text"])


def _control(terminal: Terminal, text: str) -> None:
    try:
        sent = json.loads(text)
        if sent.get("type") == "resize":
            terminal.resize(int(sent["cols"]), int(sent["rows"]))
    except (ValueError, KeyError, TypeError, AttributeError):
        pass  # Not something this page sends; nothing to do.


def _set_size(fd: int, cols: int, rows: int) -> None:
    cols, rows = max(1, min(cols, 1000)), max(1, min(rows, 1000))
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def _take_terminal(child: int) -> None:
    """In the child: a session of its own, whose controlling terminal is the pseudo-terminal, so it gets
    SIGWINCH when the size changes, and ssh can ask for a key's passphrase there. uvloop (uvicorn's loop)
    runs this before it sets up the child's stdin and applies start_new_session, so it does both itself,
    on the terminal's own descriptor."""
    os.setsid()
    fcntl.ioctl(child, termios.TIOCSCTTY, 0)


async def _ready(fd: int, *, reading: bool) -> None:
    loop = asyncio.get_running_loop()
    ready = loop.create_future()

    def wake() -> None:
        if not ready.done():
            ready.set_result(None)

    (loop.add_reader if reading else loop.add_writer)(fd, wake)
    try:
        await ready
    finally:
        (loop.remove_reader if reading else loop.remove_writer)(fd)
