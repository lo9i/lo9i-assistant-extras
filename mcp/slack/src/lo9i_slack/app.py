"""The Slack plugin's chat process. lo9i starts it (server.yaml) with the three tokens and its
channel credentials:

    SLACK_APP_TOKEN=... SLACK_BOT_TOKEN=... SLACK_USER_TOKEN=... LO9I_URL=... LO9I_CHANNEL=... \
    LO9I_CHANNEL_TOKEN=... lo9i-slack

When Slack refuses a token the process stays up without a connection, so the app shows why; when
Slack can't be reached it ends, and lo9i starts it again later.
"""

import asyncio
import logging
import os
from collections.abc import Mapping

from slack_sdk.web.async_client import AsyncWebClient

from lo9i_chat.client import ChannelClient
from lo9i_chat.runner import Runner
from lo9i_slack.channel import SlackChannel
from lo9i_slack.connection import SlackError, SlackUnreachableError, Tokens, verify
from lo9i_slack.handlers import Handlers
from lo9i_slack.sender import SlackSender
from lo9i_slack.socket_mode import Requests, connected

logger = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    asyncio.run(run(os.environ))


def tokens_from(environ: Mapping[str, str]) -> Tokens:
    return Tokens(*(environ.get(key, "").strip() for key in ("SLACK_APP_TOKEN", "SLACK_BOT_TOKEN", "SLACK_USER_TOKEN")))


async def run(environ: Mapping[str, str]) -> None:
    client = ChannelClient.from_env(environ)
    try:
        await _serve(client, tokens_from(environ))
    finally:
        await client.aclose()


async def _serve(client: ChannelClient, tokens: Tokens) -> None:
    try:
        owner = await verify(tokens)
    except SlackUnreachableError:
        raise
    except SlackError as e:
        logger.error("%s", e)
        await Runner(client, SlackChannel(client, None, str(e))).run()
        return
    web = AsyncWebClient(tokens.bot)
    async with connected(tokens.app, web, Requests(Handlers(client, web, tokens.bot, owner.id))):
        await Runner(client, SlackChannel(client, SlackSender(web, owner.id), owner)).run()


if __name__ == "__main__":
    main()
