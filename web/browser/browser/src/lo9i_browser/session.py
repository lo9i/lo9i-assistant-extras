"""The browser: one profile in the plugin's data folder, which keeps what the user signed in to, run by
Playwright without a window (headless) until the user needs to see it, to sign in to a site. Showing or
hiding the window starts the browser again on the same profile and opens the same tabs there. A computer
without a screen (Linux without DISPLAY or WAYLAND_DISPLAY) never shows one: there the browser stays
headless, and showing it means the user sees it live in lo9i's web app (live.py).

Tabs have numbers that stay while the browser runs; the last one used is the current one, which a tool
uses when it's given none."""

import asyncio
import contextlib
import logging
import os
from collections.abc import Awaitable, Callable
from pathlib import Path

from playwright.async_api import BrowserContext, Page, Playwright, ViewportSize, async_playwright
from playwright.async_api import Error as PlaywrightError

from lo9i_browser import engines
from lo9i_browser.places import has_screen

log = logging.getLogger(__name__)

_HEADLESS_SIZE: ViewportSize = {"width": 1280, "height": 900}
_ARGS = [
    # Sites, Google's sign-in among them, refuse or limit a browser that says it's automated.
    "--disable-blink-features=AutomationControlled",
    # A browser stopped with the server isn't a crash to offer restoring.
    "--hide-crash-restore-bubble",
]
_LOAD_MS = 30_000
# A page that keeps loading (a feed, ads) is read once it's this quiet, or after this long.
_SETTLE_MS = 3_000
# A tab a click opens comes a moment after it.
_NEW_TAB_MS = 1_000


class BrowserError(Exception):
    pass


class Browser:
    def __init__(self, data: Path, chrome: bool | None = None, screen: bool | None = None) -> None:
        """`data` holds the profile and the browser downloaded; `chrome` whether Google Chrome is
        installed, `screen` whether a window can show, both found on this system when not given."""
        self._data = data
        self._chrome = engines.chrome_installed() if chrome is None else chrome
        self.screen = has_screen() if screen is None else screen
        self._lock = asyncio.Lock()
        self._playwright: Playwright | None = None
        self._context: BrowserContext | None = None
        self._visible = False
        self._closing = False
        self._tabs: dict[int, Page] = {}
        self._next_id = 1
        self._current = 0
        self._ready: asyncio.Task[str] | None = None
        self._user_agent = ""
        # Called when the user closes the window: the browser is hidden again.
        self.on_window_closed: Callable[[], Awaitable[None]] | None = None

    @property
    def visible(self) -> bool:
        return self._context is not None and self._visible

    @property
    def context(self) -> BrowserContext | None:
        return self._context

    def prepare(self) -> asyncio.Task[str]:
        """Picks the browser and downloads Chromium if it's the one and isn't there yet; the server starts
        it as it starts, so the first page doesn't wait for the download."""
        if self._ready is None or (self._ready.done() and self._ready.exception()):
            self._ready = asyncio.create_task(self._prepared())
        return self._ready

    async def _prepared(self) -> str:
        engine = engines.chosen(self._data, self._chrome, engines.system_chromium() is not None)
        if engine == engines.CHROMIUM:
            await engines.install_chromium(self._data)
        return engine

    async def tab(self, number: int | None = None) -> tuple[int, Page]:
        """The tab with this number, else the current one, opening the browser when it's closed."""
        await self._running()
        if number is None:
            if self._current not in self._tabs:
                return await self._new_tab()
            number = self._current
        page = self._tabs.get(number)
        if page is None:
            raise BrowserError(f"There's no tab {number}. Open tabs: {sorted(self._tabs) or 'none'}.")
        self._current = number
        return number, page

    async def open(self, url: str, new_tab: bool = False) -> tuple[int, Page]:
        """Opens the address in the current tab, or in a new one."""
        number, page = await (self._new_tab() if new_tab else self.tab())
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=_LOAD_MS)
        except PlaywrightError as e:
            raise BrowserError(f"Couldn't open {url}: {e.message.splitlines()[0]}") from None
        await settle(page)
        return number, page

    async def tabs(self) -> list[dict]:
        listed = []
        for number, page in sorted(self._tabs.items()):
            title = ""
            with contextlib.suppress(PlaywrightError):
                title = await page.title()
            listed.append({"tab": number, "url": page.url, "title": title, "current": number == self._current})
        return listed

    async def act(self, action: Callable[[], Awaitable[None]]) -> tuple[int, Page] | None:
        """Does something on a page (a click); the tab it opened, if one opens within a moment, now the current
        one."""
        await self._running()
        assert self._context is not None
        opening = asyncio.ensure_future(self._context.wait_for_event("page", timeout=_NEW_TAB_MS))
        try:
            await action()
        except BaseException:
            opening.cancel()
            raise
        try:
            page = await opening
        except PlaywrightError:
            return None
        self._opened(page)
        await settle(page)
        return self._current, page

    async def show(self) -> None:
        """Shows the browser's window, with the tabs it had; without a screen, it's only shown live, so it
        stays as it runs."""
        async with self._lock:
            if self.visible:
                return
            if not self.screen:
                if self._context is None:
                    await self._start(visible=False, tabs=[])
                self._visible = True
                return
            await self._start(visible=True, tabs=await self._stop())

    async def hide(self) -> None:
        """Goes on without a window, with the tabs it had."""
        async with self._lock:
            if self._context is None or not self._visible:
                return
            if not self.screen:
                self._visible = False
                return
            await self._start(visible=False, tabs=await self._stop())

    async def close(self) -> None:
        async with self._lock:
            await self._stop()
            if self._playwright is not None:
                await self._playwright.stop()
                self._playwright = None

    async def _running(self) -> None:
        async with self._lock:
            if self._context is None:
                await self._start(visible=False, tabs=[])

    async def _new_tab(self) -> tuple[int, Page]:
        assert self._context is not None
        page = await self._context.new_page()
        number = self._numbered(page)
        self._current = number
        return number, page

    async def _start(self, visible: bool, tabs: list[tuple[int, str]]) -> None:
        engine = await self.prepare()
        if self._playwright is None:
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(engines.browsers_dir(self._data))
            self._playwright = await async_playwright().start()
        agent = None if visible else await self._headless_user_agent(engine)
        try:
            context = await self._playwright.chromium.launch_persistent_context(
                self._data / "profile",
                **engines.launching(engine),
                headless=not visible,
                args=_ARGS,
                ignore_default_args=["--enable-automation"],
                # A window takes the size the user gives it.
                no_viewport=visible,
                viewport=None if visible else _HEADLESS_SIZE,
                user_agent=agent,
            )
        except PlaywrightError as e:
            raise BrowserError(f"The browser didn't start: {e.message.splitlines()[0]}") from None
        self._context, self._visible = context, visible
        context.on("close", self._closed)
        await self._reopen(context, tabs)
        for page in context.pages:
            self._numbered(page)
        context.on("page", self._opened)
        if self._current not in self._tabs and self._tabs:
            self._current = min(self._tabs)

    async def _reopen(self, context: BrowserContext, tabs: list[tuple[int, str]]) -> None:
        """The tabs it had, under the same numbers; the first in the page the browser opens with."""
        opened_with = list(context.pages)
        for i, (number, url) in enumerate(tabs):
            page = opened_with[0] if i == 0 and opened_with else await context.new_page()
            self._track(number, page)
            if url != "about:blank":
                with contextlib.suppress(PlaywrightError):
                    await page.goto(url, wait_until="domcontentloaded", timeout=_LOAD_MS)

    async def _stop(self) -> list[tuple[int, str]]:
        """Closes the browser; the tabs it had: their numbers and addresses."""
        context, self._context = self._context, None
        if context is None:
            return []
        tabs = [(number, page.url) for number, page in sorted(self._tabs.items())]
        self._closing = True
        try:
            await context.close()
        finally:
            self._closing = False
            self._tabs.clear()
        return tabs

    async def _headless_user_agent(self, engine: str) -> str:
        """The browser's own user agent: without a window it says HeadlessChrome, which sites treat
        differently, and a session that seems to change browsers can be signed out."""
        if not self._user_agent:
            assert self._playwright is not None
            browser = await self._playwright.chromium.launch(**engines.launching(engine), headless=True)
            try:
                page = await browser.new_page()
                agent = await page.evaluate("navigator.userAgent")
            finally:
                await browser.close()
            self._user_agent = agent.replace("HeadlessChrome", "Chrome")
        return self._user_agent

    def _numbered(self, page: Page) -> int:
        for number, known in self._tabs.items():
            if known is page:
                return number
        number = self._next_id
        self._track(number, page)
        return number

    def _opened(self, page: Page) -> None:
        """A tab the site opened (a link to a new tab), which the user would see now."""
        self._current = self._numbered(page)

    def _track(self, number: int, page: Page) -> None:
        self._tabs[number] = page
        self._next_id = max(self._next_id, number + 1)
        page.on("close", self._tab_closed)

    def _tab_closed(self, page: Page) -> None:
        self._tabs = {n: p for n, p in self._tabs.items() if p is not page}
        if self._visible and not self._tabs and not self._closing and self._context is not None:
            # The user closed the window (a Mac keeps the browser running without one).
            asyncio.get_running_loop().create_task(self._window_closed())

    def _closed(self, context: BrowserContext) -> None:
        if self._closing or context is not self._context:
            return
        # The browser ended on its own: closing its last window on Linux, or a crash.
        was_visible = self._visible
        self._context = None
        self._tabs.clear()
        if was_visible:
            asyncio.get_running_loop().create_task(self._window_closed())

    async def _window_closed(self) -> None:
        async with self._lock:
            await self._stop()
            self._visible = False
        if self.on_window_closed is not None:
            await self.on_window_closed()


async def settle(page: Page) -> None:
    """Until the page has loaded and gone quiet, for at most a few seconds: a feed never does."""
    with contextlib.suppress(PlaywrightError):
        await page.wait_for_load_state("load", timeout=_SETTLE_MS * 3)
    with contextlib.suppress(PlaywrightError):
        await page.wait_for_load_state("networkidle", timeout=_SETTLE_MS)
