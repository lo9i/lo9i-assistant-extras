"""ChannelClient against a fake daemon (httpx.MockTransport): the routes, headers and bodies of the
channel protocol, and run streams that resume after a drop."""

import json

import httpx
import pytest

from lo9i_chat import status
from lo9i_chat.attachments import Attachment
from lo9i_chat.backoff import Backoff
from lo9i_chat.client import ChannelClient, SpeechError
from lo9i_chat.errors import DaemonUnavailableError, RefusedError
from lo9i_chat.events import ApprovalRequired, Done, Error, TextDelta

_ENV = {"LO9I_URL": "http://daemon", "LO9I_CHANNEL": "telegram", "LO9I_CHANNEL_TOKEN": "tok"}


class FakeDaemon:
    """Answers with the queued handlers in order and keeps every request."""

    def __init__(self, *handlers):
        self.requests: list[httpx.Request] = []
        self._handlers = list(handlers)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        request.read()
        self.requests.append(request)
        return self._handlers.pop(0)(request)


def _client(daemon: FakeDaemon) -> ChannelClient:
    client = ChannelClient.from_env(_ENV)
    http = httpx.AsyncClient(
        base_url=_ENV["LO9I_URL"], headers=client._http.headers, transport=httpx.MockTransport(daemon)
    )
    return ChannelClient(http, "telegram", run_retries=Backoff(first=0, attempts=3))


def _json(body, status_code=200):
    return lambda request: httpx.Response(status_code, json=body)


def _sse(*events: str):
    return lambda request: httpx.Response(200, text="".join(events), headers={"content-type": "text/event-stream"})


def _event(event_id: str, data: dict, kind: str = "") -> str:
    kind_line = f"event: {kind}\n" if kind else ""
    return f"id: {event_id}\n{kind_line}data: {json.dumps(data)}\n\n"


async def test_every_request_carries_the_token_and_client_kind():
    daemon = FakeDaemon(_json({}, 200))
    await _client(daemon).set_status([status.text("Bot: @x"), status.action("New pairing code", "new-pairing-code")])
    request = daemon.requests[0]
    assert request.method == "PUT" and request.url.path == "/channels/telegram/status"
    assert request.headers["authorization"] == "Bearer tok" and request.headers["x-lo9i-client"] == "channel"
    assert json.loads(request.content) == {
        "items": [
            {"kind": "text", "label": "", "value": "Bot: @x"},
            {"kind": "action", "label": "New pairing code", "value": "new-pairing-code"},
        ]
    }


async def test_messages_go_as_a_form_with_files():
    daemon = FakeDaemon(_json({"run_id": "r1"}))
    run_id = await _client(daemon).send_message("look", [Attachment("a.jpg", "image/jpeg", b"img")])
    request = daemon.requests[0]
    assert run_id == "r1" and request.url.path == "/channels/telegram/messages"
    assert request.headers["content-type"].startswith("multipart/form-data")
    assert b'name="text"' in request.content and b'filename="a.jpg"' in request.content


async def test_conversation_approvals_and_job_answers():
    daemon = FakeDaemon(
        _json({"thread_id": "t2"}),
        _json([{"interrupt_id": "q1", "request": {"kind": "question"}}]),
        _json({"run_id": "r2"}),
        _json({"waiting": False}),
        _json({}),
        _json({"thread_id": "home"}),
    )
    client = _client(daemon)
    assert await client.new_conversation() == "t2"
    assert await client.pending_approvals() == [ApprovalRequired("q1", {"kind": "question"})]
    assert await client.resume({"q1": {"choices": ["a"]}}) == "r2"
    assert await client.answer_job("i1", {"approved": True, "reason": ""}) is False
    await client.ack("d1", "No account is paired.")
    assert await client.go_home() == "home"
    paths = [r.url.path.removeprefix("/channels/telegram") for r in daemon.requests]
    assert paths == [
        "/conversation",
        "/approvals",
        "/approvals",
        "/job-approvals/i1",
        "/deliveries/d1",
        "/conversation",
    ]
    assert daemon.requests[-1].method == "DELETE"
    assert json.loads(daemon.requests[2].content) == {"decisions": {"q1": {"choices": ["a"]}}}
    assert json.loads(daemon.requests[4].content) == {"error": "No account is paired."}


async def test_speech_is_audio_nothing_or_an_error():
    daemon = FakeDaemon(
        lambda r: httpx.Response(200, content=b"OggS", headers={"content-type": "audio/ogg"}),
        lambda r: httpx.Response(204),
        _json({"detail": "Kokoro failed"}, 400),
    )
    client = _client(daemon)
    assert await client.speech("hi", after_voice_message=True) == b"OggS"
    assert await client.speech("hi", after_voice_message=False) is None
    with pytest.raises(SpeechError, match="Kokoro failed"):
        await client.speech("hi", after_voice_message=True)
    assert json.loads(daemon.requests[0].content) == {"text": "hi", "after_voice_message": True}


async def test_a_run_stream_resumes_after_the_last_event():
    def drop(request):
        raise httpx.ReadError("dropped")

    daemon = FakeDaemon(
        _sse(_event("1", {"type": "TextDelta", "text": "Hel"})),  # ends without `end`
        drop,
        _sse(_event("2", {"type": "TextDelta", "text": "lo"}), _event("3", {"type": "Done"}), _event("4", {}, "end")),
    )
    events = [e async for e in _client(daemon).run_events("r1")]
    assert events == [TextDelta("Hel"), TextDelta("lo"), Done()]
    assert [r.headers.get("last-event-id") for r in daemon.requests] == [None, "1", "1"]
    assert daemon.requests[0].url.path == "/channels/telegram/runs/r1/events"


async def test_a_run_stream_gives_up_after_its_attempts():
    def down(request):
        raise httpx.ConnectError("refused")

    with pytest.raises(DaemonUnavailableError):
        [e async for e in _client(FakeDaemon(down, down, down)).run_events("r1")]


async def test_a_refused_message_ends_as_an_error_event():
    daemon = FakeDaemon(_json({"detail": "A run is already going"}, 409))
    assert [e async for e in _client(daemon).message("hi")] == [Error("A run is already going")]


async def test_the_channel_stream_yields_kinds_and_data():
    daemon = FakeDaemon(
        _sse(
            ": ping\n\n",
            'event: hello\ndata: {"assistant_name": "Max"}\n\n',
            'event: action\ndata: {"action": "x"}\n\n',
        )
    )
    assert [e async for e in _client(daemon).stream()] == [
        ("hello", {"assistant_name": "Max"}),
        ("action", {"action": "x"}),
    ]


async def test_a_refused_stream_is_a_refusal():
    with pytest.raises(RefusedError) as refused:
        [e async for e in _client(FakeDaemon(_json({"detail": "Forbidden"}, 403))).stream()]
    assert refused.value.status == 403
