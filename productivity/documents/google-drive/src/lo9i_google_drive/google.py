"""Google's APIs as the user who signed in. lo9i gives the plugin the sign-in in LO9I_OAUTH (its
core/mcp/plugin_oauth.py): the refresh token of the user's own OAuth app, with the app's client ID and secret and the
token endpoint, as JSON. This trades it for access tokens, again when one runs out or Google stops taking it."""

import asyncio
import json
import os
import time
from dataclasses import dataclass
from typing import Any

import httpx

ENV = "LO9I_OAUTH"
_TIMEOUT = 60
# Taken as run out this long before Google says, so a token never ends in the middle of a call.
_MARGIN_SECONDS = 120
SIGN_IN_AGAIN = "Sign in again in google-drive's setup: Plugins in the web app, or /plugins in the terminal app."
NOT_SIGNED_IN = (
    "Not signed in to Google yet. Ask the user to sign in in google-drive's setup (open_plugin_setup opens it here): "
    "Plugins in the web app, or /plugins in the terminal app."
)


class GoogleError(Exception):
    """What Google or the sign-in said, for the model to act on."""


@dataclass(frozen=True)
class Login:
    token_url: str
    client_id: str
    client_secret: str
    refresh_token: str
    account: str = ""


def login_from_env() -> Login:
    """Raises GoogleError when the user hasn't signed in."""
    try:
        data = json.loads(os.environ.get(ENV, "") or "null")
    except ValueError:
        data = None
    if not isinstance(data, dict) or not data.get("refresh_token"):
        raise GoogleError(NOT_SIGNED_IN)
    return Login(
        str(data.get("token_url") or "https://oauth2.googleapis.com/token"),
        str(data.get("client_id", "")),
        str(data.get("client_secret", "")),
        str(data["refresh_token"]),
        str(data.get("account", "")),
    )


def client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True)


class Google:
    """Calls with an access token, refreshed when it runs out."""

    def __init__(self, login: Login) -> None:
        self.login = login
        self._token = ""
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    async def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """The answer to a call to one of Google's APIs. Raises GoogleError for one it refuses."""
        response = await self._send(method, url, await self._access_token(), kwargs)
        if response.status_code == 401:
            # Revoked or run out early: once more with a new one.
            response = await self._send(method, url, await self._access_token(renew=True), kwargs)
        if response.status_code >= 400:
            raise GoogleError(_reason(response))
        return response

    async def json(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        answer = (await self.request(method, url, **kwargs)).json()
        return answer if isinstance(answer, dict) else {}

    async def _send(self, method: str, url: str, token: str, kwargs: dict[str, Any]) -> httpx.Response:
        headers = {**kwargs.get("headers", {}), "Authorization": f"Bearer {token}"}
        rest = {k: v for k, v in kwargs.items() if k != "headers"}
        try:
            async with client() as http:
                return await http.request(method, url, headers=headers, **rest)
        except httpx.HTTPError as e:
            raise GoogleError(f"Couldn't reach Google: {e}") from e

    async def _access_token(self, renew: bool = False) -> str:
        async with self._lock:
            if renew or not self._token or time.time() >= self._expires_at - _MARGIN_SECONDS:
                await self._refresh()
            return self._token

    async def _refresh(self) -> None:
        form = {
            "grant_type": "refresh_token",
            "refresh_token": self.login.refresh_token,
            "client_id": self.login.client_id,
            **({"client_secret": self.login.client_secret} if self.login.client_secret else {}),
        }
        try:
            async with client() as http:
                response = await http.post(self.login.token_url, data=form)
        except httpx.HTTPError as e:
            raise GoogleError(f"Couldn't reach Google to sign in: {e}") from e
        answer = _json(response)
        if response.status_code != 200 or "access_token" not in answer:
            reason = answer.get("error_description") or answer.get("error") or f"HTTP {response.status_code}"
            raise GoogleError(f"Google refused the sign-in ({reason}). {SIGN_IN_AGAIN}")
        self._token = str(answer["access_token"])
        self._expires_at = time.time() + float(answer.get("expires_in", 3600))


def _json(response: httpx.Response) -> dict[str, Any]:
    try:
        answer = response.json()
    except ValueError:
        return {}
    return answer if isinstance(answer, dict) else {}


def _reason(response: httpx.Response) -> str:
    """Google's own words for a refused call: they say what to do, such as the API to turn on in the user's
    project, with its link."""
    error = _json(response).get("error")
    message = error.get("message") if isinstance(error, dict) else None
    text = str(message or response.text[:300] or f"HTTP {response.status_code}").strip()
    if response.status_code == 404:
        return f"Google found no such file, or the signed-in account can't see it: {text}"
    if response.status_code == 403 and "insufficient" in text.lower():
        return f"The sign-in doesn't allow this: {text} {SIGN_IN_AGAIN}"
    return f"Google said: {text}"
