"""One Slack conversation, as lo9i's chat operations need it, and the limits lo9i keeps there."""

import logging
from typing import Any

from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

from lo9i_slack.daemon import Limits

logger = logging.getLogger(__name__)

# chat.update allows about 50 calls a minute; a markdown block holds up to 12,000 characters.
# Bots can't send voice notes here.
LIMITS = Limits(edit_seconds=1.2, draft_limit=3500, message_limit=11000, voice=False)
# (label, button data), as lo9i sends them.
Buttons = list[list[str]]
# The most a section block holds; approval requests show their text in one.
_SECTION = 3000


class SlackChat:
    """Message ids are Slack timestamps. Markdown goes in a markdown
    block, and is resent as plain text if Slack rejects the block."""

    def __init__(self, client: AsyncWebClient, channel: str) -> None:
        self._client = client
        self._channel = channel

    async def send(self, text: str, *, markdown: bool = False, buttons: Buttons | None = None) -> str:
        blocks = _blocks(text, markdown, buttons)
        try:
            response = await self._client.chat_postMessage(
                channel=self._channel, text=text, blocks=blocks, mrkdwn=False
            )
        except SlackApiError as e:
            if not markdown:
                raise
            logger.warning("Slack rejected a markdown message: %s", e)
            response = await self._client.chat_postMessage(channel=self._channel, text=text, mrkdwn=False)
        return str(response["ts"])

    async def edit(self, message_id: str, text: str, *, markdown: bool = False, buttons: Buttons | None = None) -> None:
        try:
            await self._client.chat_update(
                channel=self._channel, ts=message_id, text=text, blocks=_blocks(text, markdown, buttons)
            )
        except SlackApiError as e:
            logger.warning("Slack edit failed: %s", e)
            if markdown:
                await self._edit_plain(message_id, text)

    async def typing(self) -> None:
        """Bots can't show typing in Slack; the streamed draft shows progress instead."""

    async def _edit_plain(self, message_id: str, text: str) -> None:
        try:
            await self._client.chat_update(channel=self._channel, ts=message_id, text=text, blocks=[])
        except SlackApiError as e:
            logger.warning("Slack edit failed: %s", e)


def _blocks(text: str, markdown: bool, buttons: Buttons | None = None) -> list[dict[str, Any]]:
    """Without blocks Slack shows `text` as is. An empty list clears blocks left by an earlier version."""
    blocks: list[dict[str, Any]] = [{"type": "markdown", "text": text}] if markdown else []
    if buttons:
        if not markdown:
            blocks.append({"type": "section", "text": {"type": "plain_text", "text": text[:_SECTION]}})
        blocks.append({"type": "actions", "elements": [_button(label, data) for label, data in buttons]})
    return blocks


def _button(label: str, data: str) -> dict[str, Any]:
    return {"type": "button", "text": {"type": "plain_text", "text": label}, "value": data, "action_id": data}
