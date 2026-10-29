"""A fake Google for every test: its token endpoint and the API calls a test answers, reached through the plugin's
own HTTP client, and the user's sign-in in LO9I_OAUTH as lo9i gives it."""

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from lo9i_google_drive import drive, google

TOKEN_URL = "https://oauth2.googleapis.com/token"
LOGIN = {
    "provider": "google",
    "token_url": TOKEN_URL,
    "client_id": "client-1",
    "client_secret": "secret-1",
    "refresh_token": "refresh-1",
    "account": "me@gmail.com",
    "scopes": ["https://www.googleapis.com/auth/drive"],
}

Answer = Callable[[httpx.Request], httpx.Response]


class FakeGoogle:
    """Answers each call with what a test said for its method and path, recording the calls."""

    def __init__(self) -> None:
        self.answers: dict[tuple[str, str], Answer] = {}
        self.calls: list[httpx.Request] = []
        self.tokens = 0
        self.refuse_refresh = False

    def on(self, method: str, path: str, answer: Answer | dict[str, Any] | str, status: int = 200) -> None:
        """`answer` is a function of the request, or the JSON (a dict) or text to answer with."""
        if callable(answer):
            self.answers[(method, path)] = answer
        elif isinstance(answer, dict):
            self.answers[(method, path)] = lambda _: httpx.Response(status, json=answer)
        else:
            self.answers[(method, path)] = lambda _: httpx.Response(status, text=answer)

    def handle(self, request: httpx.Request) -> httpx.Response:
        if str(request.url) == TOKEN_URL:
            if self.refuse_refresh:
                return httpx.Response(400, json={"error": "invalid_grant", "error_description": "Token revoked."})
            self.tokens += 1
            return httpx.Response(200, json={"access_token": f"access-{self.tokens}", "expires_in": 3599})
        self.calls.append(request)
        answer = self.answers.get((request.method, request.url.path))
        if answer is None:
            return httpx.Response(404, json={"error": {"code": 404, "message": f"No {request.url.path}"}})
        return answer(request)

    def last(self, method: str, path: str) -> httpx.Request:
        return next(r for r in reversed(self.calls) if r.method == method and r.url.path == path)


@pytest.fixture
def fake(monkeypatch) -> FakeGoogle:
    fake = FakeGoogle()
    transport = httpx.MockTransport(fake.handle)
    monkeypatch.setattr(google, "client", lambda: httpx.AsyncClient(transport=transport))
    monkeypatch.setenv(google.ENV, json.dumps(LOGIN))
    # Each test signs in afresh.
    monkeypatch.setattr(drive, "_google", None)
    return fake


def body(request: httpx.Request) -> Any:
    return json.loads(request.content)
