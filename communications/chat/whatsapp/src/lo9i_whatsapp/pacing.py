"""Keeps the account's writes far apart: WhatsApp bans accounts that write like a program."""

import asyncio
import time
from collections.abc import Awaitable, Callable


class Pacer:
    """At most one write every `gap` seconds; a write that comes sooner waits its turn."""

    def __init__(
        self,
        gap: float,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._gap = gap
        self._clock = clock
        self._sleep = sleep
        self._lock = asyncio.Lock()
        self._last: float | None = None

    async def turn(self) -> None:
        async with self._lock:
            if self._last is not None and (wait := self._last + self._gap - self._clock()) > 0:
                await self._sleep(wait)
            self._last = self._clock()
