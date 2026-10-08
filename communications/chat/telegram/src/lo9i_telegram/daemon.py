"""lo9i's channel routes, `/channels/<name>/...` (lo9i's docs/channel-plugins.md). lo9i starts the
process with LO9I_URL, LO9I_CHANNEL and LO9I_CHANNEL_TOKEN; the token reaches only this channel's routes.

The bot sends lo9i what the user does (a message, a button pressed, a command) and lo9i answers at once;
what follows comes down the stream as operations (send, edit, typing, voice) the bot carries out and
confirms with `done`."""

import contextlib
import json
import os
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any

import httpx

_CLIENT_HEADER = "X-Lo9i-Client"
# Uploads can be large; the stream stays open with long pauses, so it never times out.
_TIMEOUT = httpx.Timeout(30, read=120)
_STREAM_TIMEOUT = httpx.Timeout(30, read=None)


class DaemonError(Exception):
    """lo9i refused a request or couldn't be reached. The text says why."""


class DaemonUnavailableError(DaemonError):
    """lo9i couldn't be reached or failed on its side; trying again later can work."""


class RefusedError(DaemonError):
    """lo9i answered with a client error (4xx): the same request won't work later either."""


@dataclass(frozen=True)
class Limits:
    """How lo9i paces and sizes this channel's chats, given when the stream opens."""

    edit_seconds: float
    draft_limit: int
    message_limit: int
    voice: bool


@dataclass(frozen=True)
class Attachment:
    name: str
    media_type: str
    data: bytes


class Daemon:
    def __init__(self, http: httpx.AsyncClient, channel: str) -> None:
        """`http` has lo9i's URL as its base URL and this run's token in its headers."""
        self._http = http
        self._base = f"/channels/{channel}"

    @classmethod
    def from_env(cls, environ: Mapping[str, str] = os.environ) -> "Daemon":
        headers = {"Authorization": f"Bearer {environ['LO9I_CHANNEL_TOKEN']}", _CLIENT_HEADER: "channel"}
        http = httpx.AsyncClient(base_url=environ["LO9I_URL"], headers=headers, timeout=_TIMEOUT)
        return cls(http, environ["LO9I_CHANNEL"])

    async def aclose(self) -> None:
        await self._http.aclose()

    async def stream(self, limits: Limits) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        """(kind, data) for each event of the channel's stream, until lo9i closes it. Raises
        DaemonUnavailableError when it drops and RefusedError when lo9i won't open it."""
        params = {k: str(v).lower() if isinstance(v, bool) else str(v) for k, v in asdict(limits).items()}
        with _errors():
            async with self._http.stream("GET", f"{self._base}/stream", params=params, timeout=_STREAM_TIMEOUT) as r:
                await _raise_for_status(r)
                async for kind, data in _events(r):
                    yield kind, json.loads(data)

    async def set_status(self, items: Sequence[dict[str, str]]) -> None:
        """What the apps show for the channel: `kind` text, code, copy or action, `label` and `value`."""
        await self._request("PUT", "/status", json={"items": list(items)})

    async def done(self, operation_id: str, error: str = "", message: str = "") -> None:
        """An operation's result: what went wrong, or the id of the message it sent."""
        await self._request("POST", f"/deliveries/{operation_id}", json={"error": error, "message": message})

    async def message(self, chat: str, text: str, files: Sequence[Attachment] = (), voice: bool = False) -> None:
        """`voice`: the message was a voice note."""
        uploads = [("files", (f.name, f.data, f.media_type)) for f in files]
        form = {"chat": chat, "text": text, "voice": str(voice).lower()}
        await self._request("POST", "/messages", data=form, files=uploads)

    async def press(self, chat: str, message: str, text: str, data: str) -> None:
        """A button pressed on `message`, whose text is `text`."""
        await self._request("POST", "/presses", json={"chat": chat, "message": message, "text": text, "data": data})

    async def command(self, command: str, text: str = "") -> str:
        """`command` without its slash, and what follows it; returns what to answer."""
        response = await self._request("POST", "/commands", json={"command": command, "text": text})
        return str(response.json()["text"])

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        with _errors():
            response = await self._http.request(method, self._base + path, **kwargs)
            await _raise_for_status(response)
            return response


@contextlib.contextmanager
def _errors() -> Iterator[None]:
    try:
        yield
    except httpx.TransportError as e:
        raise DaemonUnavailableError(f"The assistant isn't reachable: {e}") from e


async def _raise_for_status(response: httpx.Response) -> None:
    if response.is_success:
        return
    await response.aread()
    try:
        detail = str(response.json().get("detail") or response.text)
    except (ValueError, AttributeError):
        detail = response.text or response.reason_phrase
    if response.is_client_error:
        raise RefusedError(detail)
    raise DaemonUnavailableError(f"The assistant failed: {detail}")


async def _events(response: httpx.Response) -> AsyncIterator[tuple[str, str]]:
    """Server-Sent Events as (kind, data). Comment lines (keep-alive pings) have no field and are skipped."""
    kind, data = "message", []
    async for line in response.aiter_lines():
        if not line:
            if data:
                yield kind, "\n".join(data)
            kind, data = "message", []
            continue
        field, _, value = line.partition(":")
        value = value[1:] if value.startswith(" ") else value
        if field == "event":
            kind = value
        elif field == "data":
            data.append(value)
