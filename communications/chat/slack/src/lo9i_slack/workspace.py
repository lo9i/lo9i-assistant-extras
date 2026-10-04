"""Reading and posting in the user's Slack workspace, as the user (the user token).

Results are short text lines for the model: where, who, when, what, and a link.
"""

from collections.abc import Awaitable
from datetime import datetime, tzinfo
from typing import Any

from slack_sdk.errors import SlackApiError, SlackClientError
from slack_sdk.web.async_client import AsyncWebClient
from slack_sdk.web.async_slack_response import AsyncSlackResponse

from lo9i_slack.connection import SlackError, SlackUnreachableError

_CONVERSATION_TYPES = "public_channel,private_channel,mpim,im"
_PAGE = 200
# A message longer than this is cut in listings; the model can read the thread for the rest.
_TEXT_LIMIT = 1500


class Workspace:
    def __init__(self, client: AsyncWebClient, zone: tzinfo) -> None:
        """`client` holds the user token."""
        self._client = client
        self._zone = zone
        self._names: dict[str, str] = {}

    async def search(self, query: str, count: int) -> list[str]:
        response = await _call(self._client.search_messages(query=query, count=count, sort="timestamp"))
        matches = (response.get("messages") or {}).get("matches", [])
        return [await self._search_line(m) for m in matches]

    async def history(self, channel: str, thread: str | None, limit: int) -> list[str]:
        channel_id = await self._channel_id(channel)
        if thread:
            response = await _call(self._client.conversations_replies(channel=channel_id, ts=thread, limit=limit))
            messages = response.get("messages") or []
        else:
            response = await _call(self._client.conversations_history(channel=channel_id, limit=limit))
            messages = list(reversed(response.get("messages") or []))
        return [await self._history_line(m) for m in messages]

    async def post(self, channel: str, text: str, thread: str | None) -> str:
        """Returns a link to the new message."""
        channel_id = await self._channel_id(channel)
        response = await _call(self._client.chat_postMessage(channel=channel_id, text=text, thread_ts=thread))
        link = await _call(self._client.chat_getPermalink(channel=channel_id, message_ts=str(response["ts"])))
        return str(link["permalink"])

    async def _channel_id(self, channel: str) -> str:
        """Accepts a channel id (C…, G…, D…) or a name, with or without #."""
        name = channel.strip().removeprefix("#")
        if name[:1] in ("C", "G", "D") and name.isupper():
            return name
        cursor = None
        while True:
            response = await _call(
                self._client.conversations_list(
                    types=_CONVERSATION_TYPES, limit=_PAGE, cursor=cursor, exclude_archived=True
                )
            )
            for conversation in response.get("channels") or []:
                if conversation.get("name") == name:
                    return conversation["id"]
            cursor = (response.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                raise SlackError(f"No channel named #{name} that you're in.")

    async def _search_line(self, match: dict[str, Any]) -> str:
        where = (match.get("channel") or {}).get("name", "?")
        who = match.get("username") or await self._user_name(match.get("user", ""))
        header = f"#{where} · {who} · {self._time(match['ts'])} · ts={match['ts']}"
        return f"{header}\n{_cut(match.get('text', ''))}\n{match.get('permalink', '')}"

    async def _history_line(self, message: dict[str, Any]) -> str:
        who = await self._user_name(message.get("user", "")) if message.get("user") else message.get("username", "bot")
        replies = f" · {message['reply_count']} replies" if message.get("reply_count") else ""
        return f"{who} · {self._time(message['ts'])} · ts={message['ts']}{replies}\n{_cut(message.get('text', ''))}"

    async def _user_name(self, user_id: str) -> str:
        if not user_id:
            return "?"
        if user_id not in self._names:
            response = await _call(self._client.users_info(user=user_id))
            user = response.get("user") or {}
            self._names[user_id] = user.get("real_name") or user.get("name") or user_id
        return self._names[user_id]

    def _time(self, ts: str) -> str:
        return datetime.fromtimestamp(float(ts), self._zone).strftime("%Y-%m-%d %H:%M")


def _cut(text: str) -> str:
    return text if len(text) <= _TEXT_LIMIT else text[:_TEXT_LIMIT] + "…"


async def _call(request: Awaitable[AsyncSlackResponse]) -> AsyncSlackResponse:
    """Awaits a Slack API call. Raises SlackError with Slack's reason."""
    try:
        return await request
    except SlackApiError as e:
        raise SlackError(f"Slack said: {e.response.get('error', 'unknown error')}") from e
    except (SlackClientError, OSError) as e:
        raise SlackUnreachableError(f"Couldn't reach Slack: {e}") from e
