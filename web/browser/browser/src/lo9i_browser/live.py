"""The browser seen live, for a computer without a screen (a Linux server): showing the browser there opens no
window, so the user sees it in lo9i's web app instead, on the plugin's page (web.py), and signs in to a site
there. Its tab is streamed as pictures (Chrome's screencast, over CDP) and the user's clicks, scrolls and
typing come back as input to the page.

While the browser shows, this server listens on a Unix socket in its data folder (live.sock, only this user
may connect); the page connects to it for each viewer, one at a time, and passes messages through its
WebSocket as they are. One JSON object per line, each way.

To the viewer:
  {"type": "frame", "data": <base64 JPEG>, "width": <CSS px>, "height": <CSS px>}
  {"type": "page", "url": ..., "title": ...}       where the tab is
  {"type": "problem", "text": ...}                 what the user asked couldn't be done
  {"type": "closed"}                               the browser isn't shown any more
From the viewer:
  {"type": "mouse", "action": "move"|"down"|"up"|"wheel", "x", "y", "button", "clicks", "dx", "dy"}
  {"type": "key", "key": "Enter"|"Control+a"}      pressed, with its modifiers
  {"type": "text", "text": ...}                    typed (or pasted)
  {"type": "go", "url": ...}, {"type": "back"}
  {"type": "done"}                                 the user is done: the browser goes back to not showing
"""

import asyncio
import contextlib
import json
import logging
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from playwright.async_api import CDPSession, Frame, Page
from playwright.async_api import Error as PlaywrightError

from lo9i_browser.session import Browser, BrowserError

log = logging.getLogger(__name__)

_SCREENCAST = {"format": "jpeg", "quality": 70, "maxWidth": 1600, "maxHeight": 1200}
_BUTTONS = {"left", "middle", "right"}
_LOAD_MS = 30_000
# A line from the viewer: a pasted text at most.
_LINE_LIMIT = 1024 * 1024


class Live:
    def __init__(self, socket: Path, address: Callable[[str], str], done: Callable[[], Awaitable[None]]) -> None:
        """`address` makes what the user typed in the address bar a web address, raising when it isn't one;
        `done` is called when the user says they're done."""
        self._socket = socket
        self._address = address
        self._done = done
        self._server: asyncio.Server | None = None
        self._viewer: asyncio.Task | None = None
        self._browser: Browser | None = None

    @property
    def listening(self) -> bool:
        return self._server is not None

    async def start(self, browser: Browser) -> None:
        """Lets the page show `browser` live, until stop()."""
        self._browser = browser
        if self._server is not None:
            return
        self._socket.parent.mkdir(parents=True, exist_ok=True)
        self._socket.unlink(missing_ok=True)
        old = os.umask(0o177)
        try:
            self._server = await asyncio.start_unix_server(self._connected, path=str(self._socket), limit=_LINE_LIMIT)
        finally:
            os.umask(old)

    async def stop(self) -> None:
        server, self._server = self._server, None
        if server is None:
            return
        server.close()
        viewer, self._viewer = self._viewer, None
        if viewer is not None and viewer is not asyncio.current_task():
            viewer.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await viewer
        self._socket.unlink(missing_ok=True)

    async def _connected(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        # One viewer at a time: the last one opened (another device, the page loaded again).
        if self._viewer is not None:
            self._viewer.cancel()
        self._viewer = asyncio.current_task()
        if self._browser is None or self._browser.context is None:
            writer.write(json.dumps({"type": "closed"}).encode() + b"\n")
            writer.close()
            return
        viewer = _Viewer(self._browser, writer, self._address)
        done = False
        try:
            await viewer.watch_current()
            done = await viewer.follow(reader)
        except (BrowserError, PlaywrightError) as e:
            log.warning("the live view ended: %s", e)
        finally:
            await viewer.end()
            if self._viewer is asyncio.current_task():
                self._viewer = None
        if done:
            await self._done()


class _Viewer:
    """One viewer's connection: the current tab streamed to it, its input to the tab."""

    def __init__(self, browser: Browser, writer: asyncio.StreamWriter, address: Callable[[str], str]) -> None:
        self._browser = browser
        self._writer = writer
        self._address = address
        self._page: Page | None = None
        self._cdp: CDPSession | None = None
        self._tasks: set[asyncio.Task] = set()
        assert browser.context is not None
        self._context = browser.context
        self._context.on("page", self._opened)

    async def follow(self, reader: asyncio.StreamReader) -> bool:
        """The viewer's input, until it leaves (False) or says it's done (True)."""
        while line := await reader.readline():
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if not isinstance(message, dict):
                continue
            if message.get("type") == "done":
                return True
            try:
                await self._input(message)
            except PlaywrightError as e:
                # The tab closed or moved on meanwhile: the next input goes to where it is now.
                log.debug("input not given: %s", e)
        return False

    async def watch_current(self) -> None:
        _, page = await self._browser.tab()
        await self._watch(page)

    async def end(self) -> None:
        self._context.remove_listener("page", self._opened)
        await self._unwatch()
        for task in self._tasks:
            task.cancel()
        with contextlib.suppress(OSError, RuntimeError):
            self._send({"type": "closed"})
            self._writer.close()

    async def _input(self, message: dict[str, Any]) -> None:
        page = self._page
        if page is None:
            return
        kind = message.get("type")
        if kind == "mouse":
            await _mouse(page, message)
        elif kind == "key" and isinstance(message.get("key"), str):
            await page.keyboard.press(message["key"])
        elif kind == "text" and isinstance(message.get("text"), str):
            await page.keyboard.type(message["text"])
        elif kind == "go" and isinstance(message.get("url"), str):
            try:
                url = self._address(message["url"])
            except Exception as e:
                self._send({"type": "problem", "text": str(e)})
                return
            self._later(page.goto(url, timeout=_LOAD_MS))
        elif kind == "back":
            self._later(page.go_back(timeout=_LOAD_MS))

    async def _watch(self, page: Page) -> None:
        """Streams this tab from now on."""
        if page is self._page:
            return
        await self._unwatch()
        self._page = page
        page.on("framenavigated", self._navigated)
        page.on("load", self._loaded)
        page.on("close", self._closed)
        cdp = await self._context.new_cdp_session(page)
        self._cdp = cdp
        cdp.on("Page.screencastFrame", lambda frame: self._later(self._frame(cdp, frame)))
        await cdp.send("Page.startScreencast", _SCREENCAST)
        await page.bring_to_front()
        await self._tell_where(page)

    async def _unwatch(self) -> None:
        page, cdp = self._page, self._cdp
        self._page = self._cdp = None
        if page is not None:
            page.remove_listener("framenavigated", self._navigated)
            page.remove_listener("load", self._loaded)
            page.remove_listener("close", self._closed)
        if cdp is not None:
            with contextlib.suppress(PlaywrightError):
                await cdp.send("Page.stopScreencast")
                await cdp.detach()

    async def _frame(self, cdp: CDPSession, frame: dict) -> None:
        """Passes a picture on, then asks for the next: a slow viewer gets fewer of them."""
        metadata = frame.get("metadata", {})
        self._send(
            {
                "type": "frame",
                "data": frame["data"],
                "width": metadata.get("deviceWidth"),
                "height": metadata.get("deviceHeight"),
            }
        )
        with contextlib.suppress(OSError, RuntimeError):
            await self._writer.drain()
        with contextlib.suppress(PlaywrightError):
            await cdp.send("Page.screencastFrameAck", {"sessionId": frame["sessionId"]})

    async def _tell_where(self, page: Page) -> None:
        title = ""
        with contextlib.suppress(PlaywrightError):
            title = await page.title()
        self._send({"type": "page", "url": page.url, "title": title})

    def _opened(self, page: Page) -> None:
        """A tab a click opened, which the browser makes the current one: the viewer sees it."""
        self._later(self._watch(page))

    def _closed(self, page: Page) -> None:
        if page is self._page:
            self._later(self._back_to_current())

    async def _back_to_current(self) -> None:
        await self._unwatch()
        # The last tab closing closes the window (session.py): the browser stops showing, and this viewer too.
        if self._browser.context is self._context and self._context.pages:
            with contextlib.suppress(BrowserError, PlaywrightError):
                await self.watch_current()

    def _navigated(self, frame: Frame) -> None:
        if frame.parent_frame is None and self._page is not None:
            self._send({"type": "page", "url": frame.url, "title": ""})

    def _loaded(self, page: Page) -> None:
        self._later(self._tell_where(page))

    def _later(self, work: Awaitable[Any]) -> None:
        task = asyncio.ensure_future(work)
        self._tasks.add(task)
        task.add_done_callback(self._finished)

    def _finished(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and isinstance(task.exception(), PlaywrightError):
            log.debug("live view: %s", task.exception())

    def _send(self, message: dict) -> None:
        if not self._writer.is_closing():
            self._writer.write(json.dumps(message).encode() + b"\n")


async def _mouse(page: Page, message: dict[str, Any]) -> None:
    x, y = _number(message.get("x")), _number(message.get("y"))
    action = message.get("action")
    button = message.get("button") if message.get("button") in _BUTTONS else "left"
    clicks = max(1, min(3, int(_number(message.get("clicks"), 1))))
    await page.mouse.move(x, y)
    if action == "down":
        await page.mouse.down(button=button, click_count=clicks)
    elif action == "up":
        await page.mouse.up(button=button, click_count=clicks)
    elif action == "wheel":
        await page.mouse.wheel(_number(message.get("dx")), _number(message.get("dy")))


def _number(value: Any, otherwise: float = 0) -> float:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else otherwise
