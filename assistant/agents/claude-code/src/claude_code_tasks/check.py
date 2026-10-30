"""lo9i's check of the plugin's field before it's saved (`check:` in server.yaml, lo9i's docs/plugin-checks.md): a
wrong Claude token otherwise shows only when the first task fails, after minutes of Claude Code's retries. It signs
in to Anthropic with the token and lists one model, which costs nothing. Without a token, Claude Code runs on lo9i's
Copilot login, which lo9i checked when its model was saved; with neither, nothing can run it.

It exits 1 saying why on stdout when the values don't work. When Anthropic can't be reached, or answers in a way that
doesn't say the token is wrong (a rate limit, a server error), it lets it through: a task would find out."""

import asyncio
import os
import sys
from collections.abc import Mapping
from typing import Any

import anthropic
import httpx2

from claude_code_tasks.runner import COPILOT_ENV, NO_LOGIN, OAUTH_PREFIX, TOKEN_ENV

_TIMEOUT = 20
# Where Claude Code takes the token, whatever an ANTHROPIC_BASE_URL around it says.
_API = "https://api.anthropic.com"
# Claude Code's sign-in tokens (`claude setup-token`) go as a bearer token with this beta, API keys as x-api-key.
_OAUTH_BETA = {"anthropic-beta": "oauth-2025-04-20"}
REFUSED = (
    "Anthropic refused the Claude token or API key. Copy it again: a token from `claude setup-token` starts with "
    "sk-ant-oat, an API key from console.anthropic.com with sk-ant-api."
)


async def problem(env: Mapping[str, str], http: httpx2.AsyncClient | None = None) -> str | None:
    """Why Claude Code couldn't run with these values; None when it can, or when that can't be told now. `http`
    reaches Anthropic, the SDK's own client when None."""
    token = env.get(TOKEN_ENV, "").strip()
    if token:
        return await _refused(token, http)
    return None if env.get(COPILOT_ENV, "").strip() else NO_LOGIN


async def _refused(token: str, http: httpx2.AsyncClient | None) -> str | None:
    options: dict[str, Any] = {"base_url": _API, "timeout": _TIMEOUT, "max_retries": 1, "http_client": http}
    async with anthropic.AsyncAnthropic(**_credentials(token), **options) as client:
        try:
            await client.models.list(limit=1)
        except anthropic.AuthenticationError:
            return REFUSED
        except anthropic.APIError:
            return None
    return None


def _credentials(token: str) -> dict[str, Any]:
    if token.startswith(OAUTH_PREFIX):
        return {"auth_token": token, "default_headers": _OAUTH_BETA}
    return {"api_key": token}


def main() -> None:
    reason = asyncio.run(problem(os.environ))
    if reason:
        print(reason)
        sys.exit(1)


if __name__ == "__main__":
    main()
