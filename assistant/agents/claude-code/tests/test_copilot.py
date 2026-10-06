"""Claude Code on the user's Copilot plan: the relay with a fake GitHub and Copilot behind it."""

import json

import httpx
import pytest

from claude_code_tasks import copilot
from claude_code_tasks.relay import Relay

MODELS = ["claude-opus-5.5", "claude-opus-5", "claude-sonnet-5.5", "claude-haiku-4.5"]


class FakeCopilot:
    """GitHub's token exchange and Copilot's API; `reject_first` makes Copilot refuse the first token."""

    def __init__(self, reject_first: bool = False) -> None:
        self.exchanges = 0
        self.messages: list[httpx.Request] = []
        self.reject_first = reject_first

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.github.com":
            self.exchanges += 1
            body = {
                "token": f"copilot-{self.exchanges}",
                "expires_at": 4e9,
                "endpoints": {"api": "https://copilot.test"},
            }
            return httpx.Response(200, json=body)
        if request.url.path == "/models":
            data = [{"id": m, "supported_endpoints": ["/v1/messages"]} for m in MODELS]
            return httpx.Response(200, json={"data": [*data, {"id": "gpt-6", "supported_endpoints": []}]})
        self.messages.append(request)
        if self.reject_first and request.headers["authorization"] == "Bearer copilot-1":
            return httpx.Response(401, json={"error": "expired"})
        return httpx.Response(
            200, text="event: message_stop\ndata: {}\n\n", headers={"content-type": "text/event-stream"}
        )


async def _post(relay: Relay, body: dict, key: str | None = None) -> httpx.Response:
    headers = {"authorization": f"Bearer {key or relay.key}", "anthropic-version": "2023-06-01", "x-api-key": "x"}
    async with httpx.AsyncClient() as http:
        return await http.post(f"http://127.0.0.1:{relay.port}/v1/messages", json=body, headers=headers)


def _user(text: str) -> dict:
    return {"model": "claude-opus-5-5-20260101", "messages": [{"role": "user", "content": text}]}


async def test_requests_are_signed_for_copilot_with_its_model_ids_and_streamed_back():
    fake = FakeCopilot()
    async with Relay("ghu_x", httpx.MockTransport(fake)).running() as relay:
        answer = await _post(relay, _user("fix the tests"))
    assert answer.status_code == 200 and "message_stop" in answer.text
    sent = fake.messages[0]
    assert str(sent.url) == "https://copilot.test/v1/messages"
    assert sent.headers["authorization"] == "Bearer copilot-1" and sent.headers["x-initiator"] == "user"
    assert sent.headers["copilot-integration-id"] == "vscode-chat" and "x-api-key" not in sent.headers
    assert json.loads(sent.content)["model"] == "claude-opus-5.5"


async def test_claude_code_is_pointed_at_the_relay_with_copilots_newest_models():
    async with Relay("ghu_x", httpx.MockTransport(FakeCopilot())).running() as relay:
        env = relay.claude_env()
    assert env["ANTHROPIC_BASE_URL"] == f"http://127.0.0.1:{relay.port}" and env["ANTHROPIC_AUTH_TOKEN"] == relay.key
    assert env["ANTHROPIC_MODEL"] == env["ANTHROPIC_DEFAULT_OPUS_MODEL"] == "claude-opus-5.5"
    assert env["ANTHROPIC_DEFAULT_SONNET_MODEL"] == "claude-sonnet-5.5"
    assert env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == "claude-haiku-4.5"


async def test_only_claude_code_with_the_relays_key_can_use_it():
    fake = FakeCopilot()
    async with Relay("ghu_x", httpx.MockTransport(fake)).running() as relay:
        answer = await _post(relay, _user("hi"), key="guess")
    assert answer.status_code == 401 and fake.messages == []


async def test_a_rejected_token_is_exchanged_again_once():
    fake = FakeCopilot(reject_first=True)
    async with Relay("ghu_x", httpx.MockTransport(fake)).running() as relay:
        answer = await _post(relay, _user("hi"))
    assert answer.status_code == 200 and fake.exchanges == 2
    assert [m.headers["authorization"] for m in fake.messages] == ["Bearer copilot-1", "Bearer copilot-2"]


def test_continuing_after_tool_results_counts_as_the_agents():
    results = {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}
    assert copilot.initiator({"messages": [results]}) == "agent"
    assert copilot.initiator({"messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]}) == "user"
    assert copilot.initiator({"messages": [{"role": "assistant", "content": "…"}]}) == "agent"
    assert copilot.rewrite(b"not json", MODELS) == (b"not json", "agent")


@pytest.mark.parametrize(
    ("asked", "given"),
    [
        ("claude-sonnet-5-5", "claude-sonnet-5.5"),
        ("claude-opus-5-5-20260101", "claude-opus-5.5"),
        ("claude-haiku-4.5", "claude-haiku-4.5"),
        ("claude-haiku-5-1-20270101", "claude-haiku-4.5"),
        ("gpt-6", "gpt-6"),
    ],
)
def test_models_are_named_the_way_copilot_does(asked, given):
    assert copilot.model_for(asked, MODELS) == given
