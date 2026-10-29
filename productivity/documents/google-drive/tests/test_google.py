"""Signing in with what lo9i gives the plugin, and calling Google with access tokens it refreshes itself."""

import httpx
import pytest

from lo9i_google_drive import google
from lo9i_google_drive.google import Google, GoogleError, login_from_env

FILES = "https://www.googleapis.com/drive/v3/files"


@pytest.mark.parametrize("value", ["", "{oops", '{"provider": "google"}'])
def test_without_a_sign_in_it_asks_the_user_to_sign_in(monkeypatch, value):
    monkeypatch.setenv(google.ENV, value)
    with pytest.raises(GoogleError, match=r"Not signed in to Google yet.*open_plugin_setup"):
        login_from_env()


def test_the_sign_in_lo9i_gives(fake):
    login = login_from_env()
    assert (login.token_url, login.client_id, login.client_secret, login.refresh_token, login.account) == (
        "https://oauth2.googleapis.com/token",
        "client-1",
        "secret-1",
        "refresh-1",
        "me@gmail.com",
    )


async def test_an_access_token_is_got_once_and_sent_with_each_call(fake):
    fake.on("GET", "/drive/v3/files", {"files": []})
    client = Google(login_from_env())
    await client.json("GET", FILES)
    await client.json("GET", FILES)
    assert fake.tokens == 1
    assert [r.headers["Authorization"] for r in fake.calls] == ["Bearer access-1", "Bearer access-1"]


async def test_a_refused_token_is_renewed_once(fake):
    fake.on(
        "GET",
        "/drive/v3/files",
        lambda r: httpx.Response(401 if r.headers["Authorization"] == "Bearer access-1" else 200, json={}),
    )
    await Google(login_from_env()).json("GET", FILES)
    assert fake.tokens == 2 and len(fake.calls) == 2


async def test_a_refused_sign_in_says_to_sign_in_again(fake):
    fake.refuse_refresh = True
    with pytest.raises(GoogleError, match=r"Google refused the sign-in \(Token revoked.\)\. Sign in again"):
        await Google(login_from_env()).json("GET", FILES)


async def test_googles_own_words_say_whats_wrong(fake):
    disabled = "Google Drive API has not been used in project 123 before or it is disabled. Enable it by visiting https://console.developers.google.com/apis/api/drive.googleapis.com/overview?project=123"
    fake.on("GET", "/drive/v3/files", {"error": {"code": 403, "message": disabled}}, status=403)
    client = Google(login_from_env())
    with pytest.raises(GoogleError, match=r"Google said: Google Drive API has not been used.*Enable it by visiting"):
        await client.json("GET", FILES)
    fake.on(
        "GET",
        "/drive/v3/files",
        {"error": {"code": 403, "message": "Request had insufficient authentication scopes."}},
        status=403,
    )
    with pytest.raises(GoogleError, match=r"The sign-in doesn't allow this.*Sign in again"):
        await client.json("GET", FILES)
    with pytest.raises(GoogleError, match="Google found no such file"):
        await client.json("GET", f"{FILES}/missing")
