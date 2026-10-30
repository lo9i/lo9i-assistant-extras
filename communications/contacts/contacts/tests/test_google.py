"""Google Contacts over CardDAV with the user's Google sign-in: a fake Google, its token endpoint and its CardDAV
server, so nothing reaches a network."""

import json
from xml.sax.saxutils import escape

import httpx2
import pytest
from test_carddav import ANA, _multistatus, _response

from lo9i_contacts import google
from lo9i_contacts.contacts import ContactsError

_PRINCIPAL = "/carddav/v1/principals/me@x.com/"
_HOME = "/carddav/v1/principals/me@x.com/lists/"
_BOOK = "/carddav/v1/principals/me@x.com/lists/default/"
LOGIN = google.Login("https://oauth2.googleapis.com/token", "client-1", "secret-1", "refresh-1")


class Google:
    """Hands out access-1, access-2, ... for refresh-1, and answers CardDAV for the latest one it handed out (or
    only after `revoked` of them were refused). Its well-known address leads to the principal, as Google's does."""

    def __init__(self, revoked: int = 0, refuse_sign_in: bool = False) -> None:
        self.tokens: list[dict] = []
        self.revoked = revoked
        self.refuse_sign_in = refuse_sign_in

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        if request.url.host == "oauth2.googleapis.com":
            return self._token(dict(httpx2.QueryParams(request.content.decode())))
        if request.headers.get("Authorization") != f"Bearer access-{len(self.tokens)}" or self.revoked:
            self.revoked = max(0, self.revoked - 1)
            return httpx2.Response(401)
        return self._dav(request.method, request.url.path, request.content.decode())

    def _token(self, form: dict) -> httpx2.Response:
        if self.refuse_sign_in or form.get("refresh_token") != "refresh-1":
            return httpx2.Response(400, json={"error": "invalid_grant", "error_description": "Token has been revoked."})
        self.tokens.append(form)
        return httpx2.Response(200, json={"access_token": f"access-{len(self.tokens)}", "expires_in": 3599})

    def _dav(self, method: str, path: str, body: str) -> httpx2.Response:
        if path == "/.well-known/carddav":
            return httpx2.Response(301, headers={"Location": _BOOK})
        if method == "PROPFIND" and "current-user-principal" in body and path == _BOOK:
            found = f"<d:current-user-principal><d:href>{_PRINCIPAL}</d:href></d:current-user-principal>"
            return httpx2.Response(207, text=_multistatus(_response(path, found)))
        if method == "PROPFIND" and path == _PRINCIPAL:
            home = f"<c:addressbook-home-set><d:href>{_HOME}</d:href></c:addressbook-home-set>"
            return httpx2.Response(207, text=_multistatus(_response(path, home)))
        if method == "PROPFIND" and path == _HOME:
            book = "<d:resourcetype><d:collection/><c:addressbook/></d:resourcetype>"
            return httpx2.Response(207, text=_multistatus(_response(_BOOK, book)))
        if method == "REPORT" and path == _BOOK:
            return httpx2.Response(
                207, text=_multistatus(_response(f"{_BOOK}1", f"<c:address-data>{escape(ANA)}</c:address-data>"))
            )
        return httpx2.Response(404)


def _contacts(server: Google):
    return google.contacts(LOGIN, httpx2.MockTransport(server))


async def test_the_signed_in_accounts_contacts_are_found_with_an_access_token():
    server = Google()
    assert [c.name for c in await _contacts(server).search("ana")] == ["Ana García"]
    assert server.tokens == [
        {
            "grant_type": "refresh_token",
            "refresh_token": "refresh-1",
            "client_id": "client-1",
            "client_secret": "secret-1",
        }
    ]


async def test_a_refused_access_token_is_renewed_once():
    server = Google(revoked=1)
    assert [c.name for c in await _contacts(server).search("ana")] == ["Ana García"]
    assert len(server.tokens) == 2


async def test_a_refused_sign_in_says_to_sign_in_again():
    with pytest.raises(ContactsError, match=r"Google refused the sign-in \(Token has been revoked\.\)\. Sign in again"):
        await _contacts(Google(refuse_sign_in=True)).check()


async def test_access_google_keeps_refusing_says_how_to_fix_it():
    with pytest.raises(ContactsError, match="CardDAV API"):
        await _contacts(Google(revoked=5)).check()


def test_the_sign_in_comes_from_lo9i_and_is_none_until_the_user_signs_in(monkeypatch):
    monkeypatch.delenv(google.ENV, raising=False)
    assert google.login_from_env() is None
    monkeypatch.setenv(google.ENV, "")
    assert google.login_from_env() is None
    login = {"provider": "google", "token_url": LOGIN.token_url, "client_id": "client-1", "client_secret": "secret-1"}
    monkeypatch.setenv(google.ENV, json.dumps({**login, "refresh_token": "refresh-1", "account": "me@x.com"}))
    assert google.login_from_env() == LOGIN
