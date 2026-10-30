"""Google Contacts over CardDAV, as the Google account the user signed in with. Google's CardDAV server takes only
OAuth, so it can't be given a user name and password: the user signs in in the plugin's setup with an OAuth app of
their own, and lo9i gives the plugin the sign-in in LO9I_OAUTH (its core/mcp/plugin_oauth.py): the refresh token, the
app's client ID and secret and the token endpoint, as JSON. This trades it for access tokens, again when one runs out
or Google stops taking it."""

import asyncio
import json
import os
import time
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import Any

import httpx2

from lo9i_contacts.carddav import CardDav
from lo9i_contacts.contacts import ContactsError

ENV = "LO9I_OAUTH"
# Where CardDAV clients start with Google (developers.google.com/people/carddav): it leads to the account's principal.
CARDDAV_URL = "https://www.googleapis.com/.well-known/carddav"
_TOKEN_URL = "https://oauth2.googleapis.com/token"
_TIMEOUT = 30
# Taken as run out this long before Google says, so a token never ends in the middle of a read.
_MARGIN_SECONDS = 120
_SIGN_IN_AGAIN = (
    "Sign in again in the contacts plugin's setup: Plugins in the web app, or /plugins in the terminal app."
)
_REFUSED = (
    "Google refused the contacts sign-in. " + _SIGN_IN_AGAIN + " Its Google Cloud project needs the CardDAV API "
    "turned on (APIs & Services → Library)."
)


@dataclass(frozen=True)
class Login:
    token_url: str
    client_id: str
    client_secret: str
    refresh_token: str


def login_from_env() -> Login | None:
    """The user's Google sign-in, or None when they haven't signed in (or signed out)."""
    try:
        data = json.loads(os.environ.get(ENV, "") or "null")
    except ValueError:
        return None
    if not isinstance(data, dict) or not data.get("refresh_token"):
        return None
    return Login(
        str(data.get("token_url") or _TOKEN_URL),
        str(data.get("client_id", "")),
        str(data.get("client_secret", "")),
        str(data["refresh_token"]),
    )


def contacts(login: Login, transport: httpx2.AsyncBaseTransport | None = None) -> CardDav:
    """The signed-in account's contacts on Google's CardDAV server."""
    return CardDav(CARDDAV_URL, GoogleAuth(login, transport), _REFUSED, transport)


class GoogleAuth(httpx2.Auth):
    """Signs each request with an access token, and once more with a new one when Google refuses it."""

    def __init__(self, login: Login, transport: httpx2.AsyncBaseTransport | None = None) -> None:
        self._login = login
        self._transport = transport
        self._token = ""
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    async def async_auth_flow(self, request: httpx2.Request) -> AsyncGenerator[httpx2.Request, httpx2.Response]:
        request.headers["Authorization"] = f"Bearer {await self._access_token()}"
        response = yield request
        if response.status_code == 401:
            # Revoked or run out early.
            request.headers["Authorization"] = f"Bearer {await self._access_token(renew=True)}"
            yield request

    async def _access_token(self, renew: bool = False) -> str:
        async with self._lock:
            if renew or not self._token or time.time() >= self._expires_at - _MARGIN_SECONDS:
                await self._refresh()
            return self._token

    async def _refresh(self) -> None:
        """Raises ContactsError when Google can't be reached or refuses the sign-in."""
        form = {
            "grant_type": "refresh_token",
            "refresh_token": self._login.refresh_token,
            "client_id": self._login.client_id,
            **({"client_secret": self._login.client_secret} if self._login.client_secret else {}),
        }
        try:
            async with httpx2.AsyncClient(timeout=_TIMEOUT, transport=self._transport) as http:
                response = await http.post(self._login.token_url, data=form)
        except httpx2.HTTPError as e:
            raise ContactsError(f"Couldn't reach Google to sign in: {e}") from e
        answer = _json(response)
        if response.status_code != 200 or "access_token" not in answer:
            reason = answer.get("error_description") or answer.get("error") or f"HTTP {response.status_code}"
            raise ContactsError(f"Google refused the sign-in ({reason}). {_SIGN_IN_AGAIN}")
        self._token = str(answer["access_token"])
        self._expires_at = time.time() + float(answer.get("expires_in", 3600))


def _json(response: httpx2.Response) -> dict[str, Any]:
    try:
        answer = response.json()
    except ValueError:
        return {}
    return answer if isinstance(answer, dict) else {}
