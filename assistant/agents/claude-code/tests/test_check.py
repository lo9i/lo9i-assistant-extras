"""The check lo9i runs before the plugin's field is saved: a fake Anthropic, so nothing reaches the network."""

import os

import httpx2
import pytest

from claude_code_tasks import check
from claude_code_tasks.runner import COPILOT_ENV, NO_LOGIN, TOKEN_ENV


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """lo9i runs the check with only the plugin's env: none of the Anthropic settings of the shell running the tests."""
    for name in [n for n in os.environ if n.startswith("ANTHROPIC_")]:
        monkeypatch.delenv(name)


class Anthropic:
    """Takes `good-key` as an API key and `sk-ant-oat-good` as a sign-in token; answers `status` to everything when
    it's set."""

    def __init__(self, status: int = 0) -> None:
        self.status = status
        self.seen: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.seen.append(request)
        if self.status:
            return httpx2.Response(self.status, json={"type": "error", "error": {"type": "x", "message": "x"}})
        # The SDK sends an API key as a bearer token too.
        key = request.headers.get("x-api-key") or request.headers.get("authorization", "").removeprefix("Bearer ")
        oauth = "oauth-2025-04-20" in request.headers.get("anthropic-beta", "")
        signed = key == "good-key" or (key == "sk-ant-oat-good" and oauth)
        if not signed:
            error = {"type": "authentication_error", "message": "invalid x-api-key"}
            return httpx2.Response(401, json={"type": "error", "error": error})
        return httpx2.Response(200, json={"data": [], "has_more": False, "first_id": None, "last_id": None})


async def _problem(env: dict[str, str], anthropic: Anthropic) -> str | None:
    async with httpx2.AsyncClient(transport=httpx2.MockTransport(anthropic)) as http:
        return await check.problem(env, http)


async def test_a_token_anthropic_takes_passes():
    api = Anthropic()
    assert await _problem({TOKEN_ENV: "good-key"}, api) is None
    assert await _problem({TOKEN_ENV: " sk-ant-oat-good "}, api) is None
    assert {r.url.path for r in api.seen} == {"/v1/models"}


async def test_a_token_anthropic_refuses_says_so():
    assert await _problem({TOKEN_ENV: "sk-ant-api-wrong"}, Anthropic()) == check.REFUSED
    assert await _problem({TOKEN_ENV: "sk-ant-oat-wrong"}, Anthropic()) == check.REFUSED


async def test_when_anthropic_cant_tell_the_token_goes_through():
    assert await _problem({TOKEN_ENV: "good-key"}, Anthropic(status=529)) is None
    assert await _problem({TOKEN_ENV: "good-key"}, Anthropic(status=429)) is None


async def test_without_a_token_it_needs_the_copilot_login():
    assert await _problem({COPILOT_ENV: "gho_x"}, Anthropic()) is None
    assert await _problem({TOKEN_ENV: " "}, Anthropic()) == NO_LOGIN
