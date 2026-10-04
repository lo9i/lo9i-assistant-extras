"""Delivers messages the assistant starts (send_message), and scheduled jobs' requests, to the owner's
DM with the bot."""

from typing import Any

from slack_sdk.errors import SlackApiError, SlackClientError
from slack_sdk.web.async_client import AsyncWebClient

from lo9i_chat.approvals import job_approval_buttons, job_approval_text
from lo9i_chat.runner import DeliveryError
from lo9i_chat.split import split_text
from lo9i_slack.port import LIMITS, SlackChat


class SlackSender:
    def __init__(self, client: AsyncWebClient, owner_id: str) -> None:
        self._client = client
        self._owner_id = owner_id

    async def send(self, text: str) -> None:
        try:
            chat = SlackChat(self._client, await self._dm())
            for part in split_text(text, LIMITS.chunk):
                await chat.send(part, markdown=True)
        except (SlackApiError, SlackClientError, OSError) as e:
            raise DeliveryError(f"Slack didn't take the message: {e}") from e

    async def forward(self, interrupt_id: str, title: str, request: dict[str, Any]) -> None:
        """A scheduled job's request, with buttons answered in handlers.py."""
        text, buttons = job_approval_text(title, request), job_approval_buttons(interrupt_id)
        try:
            await SlackChat(self._client, await self._dm()).send(text, buttons=buttons)
        except (SlackApiError, SlackClientError, OSError) as e:
            raise DeliveryError(f"Slack didn't take the request: {e}") from e

    async def _dm(self) -> str:
        channel = (await self._client.conversations_open(users=self._owner_id)).get("channel") or {}
        return str(channel["id"])
