"""Telling lo9i about the browser's window (lo9i's docs/background-work.md). While it's shown the user is
signing in, with no call in progress, and lo9i would stop this server as idle, closing the window: showing it
starts work (work_started), which holds the server, and the window closing finishes it (work_finished), which
tells the conversation that showed it, so the assistant knows the user is done."""

import logging
from typing import Any

from pydantic import BaseModel

log = logging.getLogger(__name__)

STARTED = "notifications/lo9i/work_started"
FINISHED = "notifications/lo9i/work_finished"
WORK = "window"
# Where each call says which conversation in lo9i it was made in.
ORIGIN = "lo9i/origin"


class _Notification(BaseModel):
    """A notification of lo9i's own. The session sends any model with a method and params (it dumps it),
    though its type names MCP's."""

    method: str
    params: dict[str, Any]


def origin(meta: Any) -> dict | None:
    """The conversation in lo9i a call was made in, from its `_meta`."""
    meta = meta or {}
    found = meta.get(ORIGIN) if isinstance(meta, dict) else getattr(meta, ORIGIN, None)
    return found if isinstance(found, dict) else None


class Window:
    """The window's work, told on the connection of the call that showed it."""

    def __init__(self) -> None:
        self._session: Any = None
        self._origin: dict | None = None

    async def shown(self, session: Any, origin: dict | None) -> None:
        if origin is None:
            # Not from lo9i: nobody to tell, and nothing holds the server.
            return
        if self._origin is None:
            await self._send(session, STARTED, {"work": WORK})
        self._session, self._origin = session, origin

    async def closed(self, text: str) -> None:
        session, origin = self._session, self._origin
        self._session = self._origin = None
        if origin is not None:
            await self._send(session, FINISHED, {"work": WORK, "origin": origin, "ok": True, "text": text})

    @staticmethod
    async def _send(session: Any, method: str, params: dict[str, Any]) -> None:
        try:
            await session.send_notification(_Notification(method=method, params=params))
        except Exception:
            # The connection went (lo9i stopped): the work no longer holds anything.
            log.warning("couldn't tell lo9i %s", method)
