"""Each provider through its real SDK, against canned HTTP answers."""

import asyncio
import json

import anthropic
import httpx2
import openai
import pytest

from lo9i_factory.providers import ProviderError, Providers, Request, Turn, available
from lo9i_factory.providers.anthropic_api import FALLBACK_BETA, AnthropicProvider
from lo9i_factory.providers.copilot import copilot_provider
from lo9i_factory.providers.openai_api import openai_provider

REQUEST = Request(
    "You are the architect.", [Turn("user", "Add a cache"), Turn("assistant", "Where?"), Turn("user", "Search")]
)
SCHEMA = {
    "type": "object",
    "properties": {"plan": {"type": "string"}},
    "required": ["plan"],
    "additionalProperties": False,
}


class Http:
    """Answers requests from canned (status, json) by path, and keeps them."""

    def __init__(self, answers: dict[str, list[tuple[int, dict]]]) -> None:
        self.answers = answers
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        status, body = self.answers[request.url.path].pop(0)
        return httpx2.Response(status, json=body)

    def body(self, i: int = -1) -> dict:
        return json.loads(self.requests[i].content)


def message(model="claude-opus-5-5", stop="end_turn", text="Use an LRU", **extra) -> dict:
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [{"type": "thinking", "thinking": "", "signature": "s"}, {"type": "text", "text": text}],
        "stop_reason": stop,
        "stop_sequence": None,
        "usage": {
            "input_tokens": 120,
            "output_tokens": 40,
            "cache_read_input_tokens": 3000,
            "cache_creation_input_tokens": 500,
        },
        **extra,
    }


def completion(content="Use an LRU", finish="stop", refusal=None, model="gpt-5-2025-08-07") -> dict:
    return {
        "id": "c",
        "object": "chat.completion",
        "created": 1,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content, "refusal": refusal},
                "finish_reason": finish,
            }
        ],
        "usage": {
            "prompt_tokens": 1200,
            "completion_tokens": 30,
            "total_tokens": 1230,
            "prompt_tokens_details": {"cached_tokens": 1000},
        },
    }


def claude(http: Http) -> AnthropicProvider:
    client = anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(http))
    return AnthropicProvider("sk-ant-test", http_client=client)


def gpt(http: Http):
    client = openai.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(http))
    return openai_provider("sk-test", http_client=client)


# --- Anthropic ---


def test_anthropic_sends_the_thread_with_the_cache_on():
    http = Http({"/v1/messages": [(200, message(model="claude-haiku-4-5"))]})
    reply = asyncio.run(claude(http).complete("claude-haiku-4-5", REQUEST))

    body = http.body()
    assert body["system"] == "You are the architect." and body["max_tokens"] == 16_000
    assert [m["role"] for m in body["messages"]] == ["user", "assistant", "user"]
    assert body["cache_control"] == {"type": "ephemeral"}
    assert "fallbacks" not in body and "thinking" not in body and "output_config" not in body
    assert http.requests[0].headers["x-api-key"] == "sk-ant-test"
    assert (reply.text, reply.model, reply.stop) == ("Use an LRU", "claude-haiku-4-5", "end")
    u = reply.usage
    assert (u.input_tokens, u.output_tokens, u.cache_read_tokens, u.cache_write_tokens) == (120, 40, 3000, 500)


def test_anthropic_falls_back_on_a_refusal_where_it_can():
    http = Http({"/v1/messages": [(200, message(model="claude-opus-4-8"))]})
    reply = asyncio.run(claude(http).complete("claude-opus-5-5", REQUEST))
    assert http.body()["fallbacks"] == "default"
    assert FALLBACK_BETA in http.requests[0].headers["anthropic-beta"]
    assert reply.model == "claude-opus-4-8"


def test_anthropic_structured_output():
    http = Http({"/v1/messages": [(200, message(model="claude-haiku-4-5", text='{"plan": "LRU"}'))]})
    asyncio.run(claude(http).complete("claude-haiku-4-5", Request("s", [Turn("user", "x")], schema=SCHEMA)))
    assert http.body()["output_config"] == {"format": {"type": "json_schema", "schema": SCHEMA}}


def test_anthropic_refusals_and_cut_replies():
    refused = message(
        stop="refusal", text="", stop_details={"type": "refusal", "category": "cyber", "explanation": None}
    )
    http = Http({"/v1/messages": [(200, refused), (200, message(stop="max_tokens"))]})
    provider = claude(http)
    reply = asyncio.run(provider.complete("claude-opus-5-5", REQUEST))
    assert (reply.stop, reply.detail) == ("refusal", "cyber")
    assert asyncio.run(provider.complete("claude-opus-5-5", REQUEST)).stop == "max_tokens"


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (401, "Anthropic refused ANTHROPIC_API_KEY"),
        (404, "Anthropic has no model claude-opus-9"),
        (400, "Anthropic answered 400 for claude-opus-9"),
    ],
)
def test_anthropic_errors_say_what_went_wrong(status, error):
    body = {"type": "error", "error": {"type": "x", "message": "no"}}
    http = Http({"/v1/messages": [(status, body)]})
    with pytest.raises(ProviderError, match=error):
        asyncio.run(claude(http).complete("claude-opus-9", REQUEST))


# --- OpenAI ---


def test_openai_sends_the_system_prompt_first_and_counts_the_cache():
    http = Http({"/v1/chat/completions": [(200, completion())]})
    reply = asyncio.run(gpt(http).complete("gpt-5", Request("s", [Turn("user", "x")], schema=SCHEMA)))

    body = http.body()
    assert body["messages"][0] == {"role": "system", "content": "s"}
    assert body["max_completion_tokens"] == 16_000
    assert body["response_format"]["json_schema"] == {"name": "reply", "schema": SCHEMA, "strict": True}
    assert http.requests[0].headers["authorization"] == "Bearer sk-test"
    assert (reply.text, reply.model, reply.stop) == ("Use an LRU", "gpt-5-2025-08-07", "end")
    assert (reply.usage.input_tokens, reply.usage.cache_read_tokens, reply.usage.output_tokens) == (200, 1000, 30)


def test_openai_refusals_and_cut_replies():
    http = Http(
        {
            "/v1/chat/completions": [
                (200, completion(content=None, refusal="I can't help with that")),
                (200, completion(finish="length")),
            ]
        }
    )
    provider = gpt(http)
    reply = asyncio.run(provider.complete("gpt-5", REQUEST))
    assert (reply.stop, reply.detail) == ("refusal", "I can't help with that")
    assert asyncio.run(provider.complete("gpt-5", REQUEST)).stop == "max_tokens"


def test_openai_errors_say_what_went_wrong():
    http = Http({"/v1/chat/completions": [(401, {"error": {"message": "bad key"}})]})
    with pytest.raises(ProviderError, match="OpenAI refused its key"):
        asyncio.run(gpt(http).complete("gpt-5", REQUEST))


# --- Copilot ---

TOKEN = {
    "token": "cop-1",
    "expires_at": 4_000_000_000,
    "endpoints": {"api": "https://api.individual.githubcopilot.com"},
}
# Copilot's models, with the APIs it serves each on.
MODELS = {
    "data": [
        {"id": "claude-sonnet-5.5", "supported_endpoints": ["/v1/messages", "/chat/completions"]},
        {"id": "gpt-6-sol", "supported_endpoints": ["/responses", "ws:/responses"]},
        {"id": "gemini-4-argon", "supported_endpoints": ["/chat/completions"]},
    ]
}


def response(text: str = '{"plan": "LRU"}', model: str = "gpt-6-sol", **extra) -> dict:
    """A Responses API answer."""
    return {
        "id": "resp_1",
        "object": "response",
        "created_at": 1,
        "model": model,
        "status": "completed",
        "output": [
            {
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": text, "annotations": []}],
            }
        ],
        "usage": {
            "input_tokens": 900,
            "input_tokens_details": {"cached_tokens": 800},
            "output_tokens": 20,
            "output_tokens_details": {"reasoning_tokens": 0},
            "total_tokens": 920,
        },
        "parallel_tool_calls": False,
        "tool_choice": "auto",
        "tools": [],
        **extra,
    }


def copilot(http: Http):
    return copilot_provider("gho_lo9i", transport=httpx2.MockTransport(http))


def test_copilot_exchanges_the_github_token_once_and_calls_the_accounts_host():
    http = Http(
        {
            "/copilot_internal/v2/token": [(200, TOKEN)],
            "/models": [(200, MODELS)],
            "/chat/completions": [(200, completion(model="gemini-4-argon"))] * 2,
        }
    )
    provider = copilot(http)

    async def twice():
        return [await provider.complete("gemini-4-argon", REQUEST) for _ in range(2)]

    replies = asyncio.run(twice())

    exchange, models, chat, _ = http.requests
    assert exchange.headers["authorization"] == "token gho_lo9i"
    assert models.headers["authorization"] == "Bearer cop-1"
    assert chat.url.host == "api.individual.githubcopilot.com"
    assert chat.headers["authorization"] == "Bearer cop-1"
    assert (chat.headers["x-initiator"], chat.headers["copilot-integration-id"]) == ("user", "vscode-chat")
    assert replies[0].model == "gemini-4-argon"


def test_copilot_asks_each_model_on_the_api_that_keeps_to_the_json():
    http = Http(
        {
            "/copilot_internal/v2/token": [(200, TOKEN)],
            "/models": [(200, MODELS)],
            "/v1/messages": [(200, message(model="claude-sonnet-5-5", text='{"plan": "LRU"}'))],
            "/responses": [(200, response())],
        }
    )
    provider = copilot(http)
    request = Request("s", [Turn("user", "x")], schema=SCHEMA)

    async def both():
        return [await provider.complete(m, request) for m in ("claude-sonnet-5.5", "gpt-6-sol")]

    claude, gpt = asyncio.run(both())

    messages = next(r for r in http.requests if r.url.path == "/v1/messages")
    assert messages.url.host == "api.individual.githubcopilot.com"
    assert messages.headers["authorization"] == "Bearer cop-1"
    assert json.loads(messages.content)["output_config"]["format"]["schema"] == SCHEMA
    assert (claude.text, claude.model) == ('{"plan": "LRU"}', "claude-sonnet-5-5")

    responses = json.loads(next(r for r in http.requests if r.url.path == "/responses").content)
    assert responses["instructions"] == "s" and responses["input"] == [{"role": "user", "content": "x"}]
    assert responses["text"]["format"] == {"type": "json_schema", "name": "reply", "schema": SCHEMA, "strict": True}
    assert (gpt.text, gpt.stop) == ('{"plan": "LRU"}', "end")
    assert (gpt.usage.input_tokens, gpt.usage.cache_read_tokens, gpt.usage.output_tokens) == (100, 800, 20)
    # Copilot's models are read once.
    assert [r.url.path for r in http.requests].count("/models") == 1


def test_a_responses_reply_cut_short():
    cut = response(status="incomplete", incomplete_details={"reason": "max_output_tokens"})
    http = Http({"/copilot_internal/v2/token": [(200, TOKEN)], "/models": [(200, MODELS)], "/responses": [(200, cut)]})
    assert asyncio.run(copilot(http).complete("gpt-6-sol", REQUEST)).stop == "max_tokens"


def test_copilot_exchanges_again_when_the_token_is_refused():
    http = Http(
        {
            "/copilot_internal/v2/token": [(200, TOKEN), (200, {**TOKEN, "token": "cop-2"})],
            "/models": [(200, MODELS)],
            "/chat/completions": [(401, {"error": {"message": "expired"}}), (200, completion())],
        }
    )
    reply = asyncio.run(copilot(http).complete("gemini-4-argon", REQUEST))
    assert reply.text == "Use an LRU"
    assert http.requests[-1].headers["authorization"] == "Bearer cop-2"


def test_copilot_says_when_the_sign_in_is_refused():
    http = Http({"/copilot_internal/v2/token": [(401, {"message": "Bad credentials"})]})
    with pytest.raises(ProviderError, match="sign in to GitHub Copilot again"):
        asyncio.run(copilot(http).complete("gpt-5", REQUEST))


# --- Keys ---


def test_a_provider_without_a_key_says_where_to_enter_it():
    providers = Providers(env={"OPENAI_API_KEY": "sk"})
    assert type(providers.get("openai")).__name__ == "ChatProvider"
    assert providers.get("openai") is providers.get("openai")
    with pytest.raises(ProviderError, match="ANTHROPIC_API_KEY isn't set: enter it in the factory plugin"):
        providers.get("anthropic")
    with pytest.raises(ProviderError, match="choose GitHub Copilot as the assistant's model"):
        providers.get("copilot")
    assert available({"OPENAI_API_KEY": "sk", "ANTHROPIC_API_KEY": ""}) == {
        "anthropic": False,
        "openai": True,
        "copilot": False,
    }
