"""The thread each message is in, so what answers a message in a thread goes in that thread too: asked of Slack the
first time, then kept, with the bot's own messages."""

import logging
from collections import OrderedDict

from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

logger = logging.getLogger(__name__)

# Messages whose thread is kept; one older than these is asked of Slack again.
_KEPT = 500


class Threads:
    def __init__(self) -> None:
        # (channel, message) → the timestamp of its thread's first message; "" when it's in none.
        self._roots: OrderedDict[tuple[str, str], str] = OrderedDict()

    async def root(self, web: AsyncWebClient, channel: str, message: str) -> str:
        """The thread `message` is in, "" for none: what answers a message outside a thread goes outside one too.
        One Slack can't say about is in none."""
        if not message:
            return ""
        if (channel, message) not in self._roots:
            try:
                self.keep(channel, message, await _asked(web, channel, message))
            except SlackApiError as e:
                logger.warning("Couldn't read the thread of message %s: %s", message, e)
                return ""
        return self._roots[(channel, message)]

    def keep(self, channel: str, message: str, root: str) -> None:
        self._roots[(channel, message)] = root
        self._roots.move_to_end((channel, message))
        while len(self._roots) > _KEPT:
            self._roots.popitem(last=False)


async def _asked(web: AsyncWebClient, channel: str, message: str) -> str:
    """conversations.replies starts with the thread's first message, which names the thread when it has one."""
    response = await web.conversations_replies(channel=channel, ts=message, limit=1)
    first = (response.get("messages") or [{}])[0]
    return str(first.get("thread_ts", ""))
