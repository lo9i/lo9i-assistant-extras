"""The Slack app's tokens, checked with Slack, and its manifest.

One Slack app, created from `manifest`, gives three tokens: the app token opens the Socket Mode
connection (no public URL needed), the bot token chats in DMs, and the user token reads and posts in
the workspace as the user. The user token's owner is the only person the bot answers.
"""

import json
from dataclasses import dataclass

from slack_sdk.errors import SlackApiError, SlackClientError
from slack_sdk.web.async_client import AsyncWebClient
from slack_sdk.web.async_slack_response import AsyncSlackResponse

from lo9i_slack.i18n import Language, translatable

# The tokens' names, in what's said about them.
_APP_TOKEN = translatable("app token")
_BOT_TOKEN = translatable("bot token")
_USER_TOKEN = translatable("user token")
_BOT_SCOPES = ["chat:write", "commands", "files:read", "im:history", "im:read", "users:read"]
_USER_SCOPES = [
    "channels:history",
    "channels:read",
    "chat:write",
    "groups:history",
    "groups:read",
    "im:history",
    "im:read",
    "mpim:history",
    "mpim:read",
    "search:read",
    "users:read",
]


class SlackError(Exception):
    """A token was rejected or a call failed. The text says which and why, in English; `shown` says it in the
    user's language."""

    def __init__(self, text: str, /, **values: str) -> None:
        super().__init__(text.format(**values) if values else text)
        self._text, self._values = text, values

    def shown(self, t: Language) -> str:
        """Its values translated too: a token's name is."""
        return t(self._text, **{name: t(value) for name, value in self._values.items()})


class SlackUnreachableError(SlackError):
    """Slack couldn't be reached; trying again later can work."""


@dataclass(frozen=True)
class Tokens:
    app: str
    bot: str
    user: str


@dataclass(frozen=True)
class Owner:
    """The workspace, and the Slack user who installed the app: the only one the bot answers."""

    team: str
    id: str
    name: str


# Until lo9i says which commands there are (its stream's `hello`): those it always has. manifest.json, which the
# app is first created from, is the manifest with these.
_COMMANDS = [
    {"command": "clear", "description": "Start a new conversation with your next message"},
    {"command": "pin", "description": "Keep a skill's instructions on in this conversation: /pin <skill>"},
    {"command": "unpin", "description": "Turn a pinned skill off in this conversation: /unpin <skill>"},
]


def manifest(name: str, commands: list[dict[str, str]] | None = None, t: Language | None = None) -> str:
    """The app manifest to paste in Slack's "Create New App → From a manifest". Slack offers only the
    slash commands it lists, so when lo9i's commands change (an agent installed), the app's manifest is
    updated with this one. Its description is in `t`'s language, English without one."""
    t = t or Language()
    app = {
        "display_information": {"name": name, "description": t("Personal assistant")},
        "features": {
            "app_home": {
                "home_tab_enabled": False,
                "messages_tab_enabled": True,
                "messages_tab_read_only_enabled": False,
            },
            "bot_user": {"display_name": name, "always_online": True},
            "slash_commands": [_slash(c) for c in commands or _COMMANDS],
        },
        "oauth_config": {"scopes": {"bot": _BOT_SCOPES, "user": _USER_SCOPES}},
        "settings": {
            "event_subscriptions": {"bot_events": ["message.im"]},
            "interactivity": {"is_enabled": True},
            "org_deploy_enabled": False,
            "socket_mode_enabled": True,
            "token_rotation_enabled": False,
        },
    }
    return json.dumps(app, indent=2)


def _slash(command: dict[str, str]) -> dict[str, object]:
    return {"command": f"/{command['command']}", "description": command["description"], "should_escape": False}


async def verify(tokens: Tokens) -> Owner:
    """Checks each token with Slack. Raises SlackError, SlackUnreachableError when Slack can't be reached."""
    _check_prefix(_APP_TOKEN, tokens.app, "xapp-")
    _check_prefix(_BOT_TOKEN, tokens.bot, "xoxb-")
    _check_prefix(_USER_TOKEN, tokens.user, "xoxp-")
    await _call(_APP_TOKEN, AsyncWebClient(tokens.app), "apps.connections.open")
    bot = await _call(_BOT_TOKEN, AsyncWebClient(tokens.bot), "auth.test")
    user = await _call(_USER_TOKEN, AsyncWebClient(tokens.user), "auth.test")
    if bot["team_id"] != user["team_id"]:
        raise SlackError(translatable("The bot token and the user token are from different workspaces."))
    return Owner(team=str(user["team"]), id=str(user["user_id"]), name=str(user["user"]))


def _check_prefix(label: str, token: str, prefix: str) -> None:
    if not token.startswith(prefix):
        raise SlackError(translatable("The {token} should start with {prefix}"), token=label, prefix=prefix)


async def _call(label: str, client: AsyncWebClient, method: str) -> AsyncSlackResponse:
    try:
        return await client.api_call(method)
    except SlackApiError as e:
        error = str(e.response.get("error", "unknown error"))
        raise SlackError(translatable("Slack rejected the {token}: {error}"), token=label, error=error) from e
    except (SlackClientError, OSError) as e:
        raise SlackUnreachableError(f"Couldn't reach Slack: {e}") from e
