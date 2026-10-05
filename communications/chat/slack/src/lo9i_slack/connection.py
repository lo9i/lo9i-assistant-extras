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
    """A token was rejected or a call failed. The text says which and why."""


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


def manifest(name: str) -> str:
    """The app manifest to paste in Slack's "Create New App → From a manifest"."""
    app = {
        "display_information": {"name": name, "description": "Personal assistant"},
        "features": {
            "app_home": {
                "home_tab_enabled": False,
                "messages_tab_enabled": True,
                "messages_tab_read_only_enabled": False,
            },
            "bot_user": {"display_name": name, "always_online": True},
            "slash_commands": [
                {"command": "/new", "description": "Start a side conversation", "should_escape": False},
                {"command": "/home", "description": "Go back to the home conversation", "should_escape": False},
            ],
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


async def verify(tokens: Tokens) -> Owner:
    """Checks each token with Slack. Raises SlackError, SlackUnreachableError when Slack can't be reached."""
    _check_prefix("App token", tokens.app, "xapp-")
    _check_prefix("Bot token", tokens.bot, "xoxb-")
    _check_prefix("User token", tokens.user, "xoxp-")
    await _call("App token", AsyncWebClient(tokens.app), "apps.connections.open")
    bot = await _call("Bot token", AsyncWebClient(tokens.bot), "auth.test")
    user = await _call("User token", AsyncWebClient(tokens.user), "auth.test")
    if bot["team_id"] != user["team_id"]:
        raise SlackError("The bot token and the user token are from different workspaces.")
    return Owner(team=str(user["team"]), id=str(user["user_id"]), name=str(user["user"]))


def _check_prefix(label: str, token: str, prefix: str) -> None:
    if not token.startswith(prefix):
        raise SlackError(f"{label} should start with {prefix}")


async def _call(label: str, client: AsyncWebClient, method: str) -> AsyncSlackResponse:
    try:
        return await client.api_call(method)
    except SlackApiError as e:
        raise SlackError(f"Slack rejected the {label.lower()}: {e.response.get('error', 'unknown error')}") from e
    except (SlackClientError, OSError) as e:
        raise SlackUnreachableError(f"Couldn't reach Slack: {e}") from e
