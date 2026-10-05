"""The daemon's channel routes, `/channels/<name>/...`, for a channel plugin's process. The daemon
starts the process with LO9I_URL, LO9I_CHANNEL and LO9I_CHANNEL_TOKEN; the token reaches only this
channel's routes."""

import asyncio
import json
import os
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from typing import Any

import httpx

from lo9i_chat.attachments import Attachment
from lo9i_chat.backoff import Backoff
from lo9i_chat.errors import DaemonError, DaemonUnavailableError, RefusedError, daemon_errors, raise_for_status
from lo9i_chat.events import ApprovalRequired, Error, Event, event_from_dict
from lo9i_chat.sse import read_events
from lo9i_chat.status import SetupItem

CLIENT_HEADER = "X-Lo9i-Client"
CLIENT_KIND = "channel"
# The kind of a run's last event.
END = "end"
# Uploads can be large; streams stay open with long pauses (a slow tool call), so they never time out.
_TIMEOUT = httpx.Timeout(30, read=120)
_STREAM_TIMEOUT = httpx.Timeout(30, read=None)
# A run's events are worth waiting for a while: the run goes on in the daemon whether we read or not.
_RUN_RETRIES = Backoff(first=0.5, most=5, attempts=8)


class SpeechError(DaemonError):
    """The daemon couldn't turn the reply into speech."""


class ChannelClient:
    def __init__(self, http: httpx.AsyncClient, channel: str, run_retries: Backoff = _RUN_RETRIES) -> None:
        """`http` has the daemon's URL as its base URL and this run's token in its headers."""
        self._http = http
        self._base = f"/channels/{channel}"
        self._run_retries = run_retries

    @classmethod
    def from_env(cls, environ: Mapping[str, str] = os.environ) -> "ChannelClient":
        headers = {"Authorization": f"Bearer {environ['LO9I_CHANNEL_TOKEN']}", CLIENT_HEADER: CLIENT_KIND}
        http = httpx.AsyncClient(base_url=environ["LO9I_URL"], headers=headers, timeout=_TIMEOUT)
        return cls(http, environ["LO9I_CHANNEL"])

    async def aclose(self) -> None:
        await self._http.aclose()

    async def stream(self) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        """(kind, data) for each event of the channel's stream, until the daemon closes it. Raises
        DaemonUnavailableError when it drops and RefusedError when the daemon won't open it."""
        async for _, kind, data in self._sse("/stream"):
            yield kind, json.loads(data)

    async def set_status(self, items: Sequence[SetupItem]) -> None:
        await self._request("PUT", "/status", json={"items": [item.to_dict() for item in items]})

    async def ack(self, delivery_id: str, error: str = "") -> None:
        """The result of a `deliver` or `approval` event; an empty error means delivered."""
        await self._request("POST", f"/deliveries/{delivery_id}", json={"error": error})

    async def new_conversation(self) -> str:
        """Starts a side conversation (the channel's /new) and returns its thread id."""
        return str((await self._request("POST", "/conversation")).json()["thread_id"])

    async def go_home(self) -> str:
        """Moves the channel back to the home conversation (its /home) and returns its thread id."""
        return str((await self._request("DELETE", "/conversation")).json()["thread_id"])

    async def send_message(self, text: str, files: Sequence[Attachment] = ()) -> str:
        """Sends a user message in the channel's conversation and returns the run's id."""
        uploads = [("files", (f.name, f.data, f.media_type)) for f in files]
        response = await self._request("POST", "/messages", data={"text": text}, files=uploads)
        return str(response.json()["run_id"])

    async def resume(self, decisions: dict[str, Any]) -> str:
        """Answers approvals or questions the conversation waits for and returns the run's id."""
        return str((await self._request("POST", "/approvals", json={"decisions": decisions})).json()["run_id"])

    async def pending_approvals(self) -> list[ApprovalRequired]:
        waiting = (await self._request("GET", "/approvals")).json()
        return [ApprovalRequired(a["interrupt_id"], a["request"]) for a in waiting]

    async def answer_job(self, interrupt_id: str, decision: dict[str, Any]) -> bool:
        """Answers a scheduled job's request. False when its run already went on without it."""
        response = await self._request("POST", f"/job-approvals/{interrupt_id}", json=decision)
        return bool(response.json()["waiting"])

    async def speech(self, text: str, after_voice_message: bool) -> bytes | None:
        """The reply as OGG Opus voice, or None when Settings → Audio says it isn't spoken."""
        body = {"text": text, "after_voice_message": after_voice_message}
        try:
            response = await self._request("POST", "/speech", json=body)
        except RefusedError as e:
            raise SpeechError(str(e)) from e
        return None if response.status_code == 204 else response.content

    async def run_events(self, run_id: str) -> AsyncIterator[Event]:
        """A run's events until its end. A dropped stream is reopened after the last event received,
        so nothing is missed or repeated. Raises DaemonError when it can't be read any more."""
        last_id, failures = "", 0
        while True:
            try:
                async for event_id, kind, data in self._sse(f"/runs/{run_id}/events", last_id):
                    last_id, failures = event_id or last_id, 0
                    if kind == END:
                        return
                    yield event_from_dict(json.loads(data))
            except DaemonUnavailableError:
                if self._run_retries.gives_up(failures + 1):
                    raise
            failures += 1
            await asyncio.sleep(self._run_retries.delay(failures))

    def message(self, text: str, files: Sequence[Attachment] = ()) -> AsyncIterator[Event]:
        """Sends a message and follows its run. A failure ends the events with an Error, so the chat
        shows it like any other failed run."""
        return self._follow(lambda: self.send_message(text, files))

    def answer(self, decisions: dict[str, Any]) -> AsyncIterator[Event]:
        """Like `message`, for answers to approvals and questions."""
        return self._follow(lambda: self.resume(decisions))

    async def _follow(self, start: Callable[[], Awaitable[str]]) -> AsyncIterator[Event]:
        try:
            run_id = await start()
            async for event in self.run_events(run_id):
                yield event
        except DaemonError as e:
            yield Error(str(e))

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        with daemon_errors():
            response = await self._http.request(method, self._base + path, **kwargs)
            response.raise_for_status()
            return response

    async def _sse(self, path: str, last_id: str = "") -> AsyncIterator[tuple[str, str, str]]:
        headers = {"Last-Event-ID": last_id} if last_id else {}
        with daemon_errors():
            async with self._http.stream("GET", self._base + path, headers=headers, timeout=_STREAM_TIMEOUT) as r:
                await raise_for_status(r)
                async for item in read_events(r):
                    yield item
