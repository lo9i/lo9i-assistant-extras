"""Claude Code on the user's Copilot plan: the relay, with a fake GitHub and
Copilot behind it, and Claude Code pointed at it."""

import asyncio
import json

import httpx2
import pytest

from lo9i_factory.agents import Job
from lo9i_factory.agents import claude_code as claude_code_module
from lo9i_factory.agents.claude_code import ClaudeCode
from lo9i_factory.agents.relay import Relay, initiator, model_for, rewrite
from lo9i_factory.providers import ProviderError
from lo9i_factory.sandbox import Done

MODELS = ["claude-opus-5.5", "claude-opus-5", "claude-sonnet-5.5", "claude-haiku-4.5"]


class FakeCopilot:
    """GitHub's token exchange and Copilot's API; `reject_first` makes Copilot refuse the first token."""

    def __init__(self, reject_first: bool = False, exchange_status: int = 200) -> None:
        self.exchanges = 0
        self.messages: list[httpx2.Request] = []
        self.reject_first = reject_first
        self.exchange_status = exchange_status

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        if request.url.host == "api.github.com":
            self.exchanges += 1
            body = {
                "token": f"copilot-{self.exchanges}",
                "expires_at": 4e9,
                "endpoints": {"api": "https://copilot.test"},
            }
            return httpx2.Response(self.exchange_status, json=body)
        if request.url.path == "/models":
            data = [{"id": m, "supported_endpoints": ["/v1/messages"]} for m in MODELS]
            return httpx2.Response(200, json={"data": [*data, {"id": "gpt-6", "supported_endpoints": []}]})
        self.messages.append(request)
        if self.reject_first and request.headers["authorization"] == "Bearer copilot-1":
            return httpx2.Response(401, json={"error": "expired"})
        return httpx2.Response(
            200, text="event: message_stop\ndata: {}\n\n", headers={"content-type": "text/event-stream"}
        )


def relay(fake: FakeCopilot) -> Relay:
    return Relay("gho_lo9i", httpx2.MockTransport(fake))


async def post(base_url: str, key: str, body: dict) -> httpx2.Response:
    headers = {"authorization": f"Bearer {key}", "anthropic-version": "2023-06-01", "x-api-key": "x"}
    async with httpx2.AsyncClient() as http:
        return await http.post(f"{base_url}/v1/messages", json=body, headers=headers)


def user(text: str, model: str = "claude-opus-5-5-20260101") -> dict:
    return {"model": model, "messages": [{"role": "user", "content": text}]}


def test_requests_are_signed_for_copilot_with_its_model_ids_and_streamed_back():
    fake = FakeCopilot()

    async def go():
        async with relay(fake).running() as r:
            return r, await post(f"http://127.0.0.1:{r.port}", r.key, user("fix the tests"))

    r, answer = asyncio.run(go())
    assert answer.status_code == 200 and "message_stop" in answer.text
    sent = fake.messages[0]
    assert str(sent.url) == "https://copilot.test/v1/messages"
    assert sent.headers["authorization"] == "Bearer copilot-1" and sent.headers["x-initiator"] == "user"
    assert sent.headers["copilot-integration-id"] == "vscode-chat" and "x-api-key" not in sent.headers
    assert json.loads(sent.content)["model"] == "claude-opus-5.5"
    assert r.requests == 1


def test_only_claude_code_with_the_relays_key_can_use_it():
    fake = FakeCopilot()

    async def go():
        async with relay(fake).running() as r:
            return await post(f"http://127.0.0.1:{r.port}", "guess", user("hi"))

    assert asyncio.run(go()).status_code == 401 and fake.messages == []


def test_a_rejected_token_is_exchanged_again_once():
    fake = FakeCopilot(reject_first=True)

    async def go():
        async with relay(fake).running() as r:
            return await post(f"http://127.0.0.1:{r.port}", r.key, user("hi"))

    assert asyncio.run(go()).status_code == 200 and fake.exchanges == 2
    assert [m.headers["authorization"] for m in fake.messages] == ["Bearer copilot-1", "Bearer copilot-2"]


def test_no_copilot_token_means_no_relay():
    async def go():
        async with relay(FakeCopilot(exchange_status=401)).running():
            pass

    with pytest.raises(ProviderError, match="sign in to GitHub Copilot again"):
        asyncio.run(go())


def test_claude_code_is_pointed_at_the_relay_from_here_or_from_the_container():
    async def go():
        async with relay(FakeCopilot()).running() as r:
            return r, r.claude_env("claude-sonnet-5.5", docker=False), r.claude_env("claude-sonnet-5.5", docker=True)

    r, local, docker = asyncio.run(go())
    assert local["ANTHROPIC_BASE_URL"] == f"http://127.0.0.1:{r.port}" and local["ANTHROPIC_AUTH_TOKEN"] == r.key
    assert docker["ANTHROPIC_BASE_URL"] == f"http://host.docker.internal:{r.port}"
    assert (
        local["ANTHROPIC_MODEL"] == "claude-sonnet-5.5" and local["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == "claude-haiku-4.5"
    )
    assert "ANTHROPIC_API_KEY" not in local and "gho_lo9i" not in local.values()


def test_the_implementers_model_must_be_one_copilot_has():
    r = relay(FakeCopilot())
    r.models = MODELS
    assert r.model("claude-sonnet-5-5") == r.model("claude-sonnet-5.5") == "claude-sonnet-5.5"
    with pytest.raises(ProviderError, match=r"no Claude model gpt-6 for Claude Code; it has claude-opus-5\.5"):
        r.model("gpt-6")


def test_continuing_after_tool_results_counts_as_the_agents():
    results = {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}
    assert initiator({"messages": [results]}) == "agent"
    assert initiator({"messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]}) == "user"
    assert initiator({"messages": [{"role": "assistant", "content": "…"}]}) == "agent"
    reminder = {"role": "system", "content": [{"type": "text", "text": "reminder"}]}
    assert initiator({"messages": [{"role": "user", "content": "hi"}, reminder]}) == "user"
    assert initiator({"messages": [results, reminder]}) == "agent"
    assert initiator({}) == "agent"
    assert rewrite(b"not json", MODELS) == (b"not json", "agent")


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
    assert model_for(asked, MODELS) == given


class RelayedBox:
    """A sandbox whose 'Claude Code' sends one message through the relay it was given, then ends."""

    def __init__(self) -> None:
        self.mode, self.worktree, self.folder = "local", "/repo/wt", "/repo/wt"

    async def run(self, argv, env=None, *, stdin=None, on_line=None, should_stop=lambda: False, timeout):
        self.seen = (argv, env)
        answer = await post(
            env["ANTHROPIC_BASE_URL"], env["ANTHROPIC_AUTH_TOKEN"], user(stdin, argv[argv.index("--model") + 1])
        )
        assert answer.status_code == 200
        end = {"type": "result", "subtype": "success", "result": "", "total_cost_usd": 0.9,
               "structured_output": {"summary": "Done", "points": []}}  # fmt: skip
        on_line(json.dumps(end) + "\n")
        return Done(0, "", 1.0)


def test_claude_code_on_copilot_runs_through_the_relay_and_counts_its_requests(tmp_path, monkeypatch):
    monkeypatch.setenv("FACTORY_DB", str(tmp_path / "factory.db"))
    fake = FakeCopilot()
    monkeypatch.setattr(claude_code_module, "Relay", lambda token: Relay(token, httpx2.MockTransport(fake)))
    monkeypatch.setattr("lo9i_factory.agents.claude_code.find", lambda name: f"/bin/{name}")
    box = RelayedBox()
    job = Job(1, 7, "Build", "sys", "claude-sonnet-5-5", None, box, provider="copilot")

    r = asyncio.run(ClaudeCode("gho_lo9i").run(job, lambda event: None, lambda: False))

    assert (r.ok, r.summary, r.requests) == (True, "Done", 1)
    argv, env = box.seen
    assert argv[argv.index("--model") + 1] == "claude-sonnet-5.5" and "--max-budget-usd" not in argv
    assert "ANTHROPIC_API_KEY" not in env and env["MCP_TOOL_TIMEOUT"]
    assert json.loads(fake.messages[0].content)["model"] == "claude-sonnet-5.5"
