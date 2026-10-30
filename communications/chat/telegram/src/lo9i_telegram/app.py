"""The Telegram plugin's process. lo9i starts it (server.yaml) with the bot token, the data folder
and its channel credentials:

    TELEGRAM_TOKEN=... TELEGRAM_DATA=... LO9I_URL=... LO9I_CHANNEL=... LO9I_CHANNEL_TOKEN=... lo9i-telegram

When Telegram refuses the token the process stays up without a bot, so the app shows why; any other
failure to start ends it, and lo9i starts it again later.
"""

import asyncio
import logging
import os
from collections.abc import Mapping
from pathlib import Path

from telegram.error import InvalidToken

from lo9i_telegram.bot import build_application, polling
from lo9i_telegram.channel import TelegramChannel
from lo9i_telegram.daemon import Daemon
from lo9i_telegram.handlers import Handlers
from lo9i_telegram.i18n import Language
from lo9i_telegram.pairing import Pairing
from lo9i_telegram.port import LIMITS
from lo9i_telegram.runner import Runner

logger = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    # Every poll would be logged at INFO.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(run(os.environ))


async def run(environ: Mapping[str, str]) -> None:
    daemon = Daemon.from_env(environ)
    pairing = await Pairing.load(Path(environ["TELEGRAM_DATA"]))
    try:
        await _serve(daemon, pairing, environ["TELEGRAM_TOKEN"].strip())
    finally:
        pairing.close()
        await daemon.aclose()


async def _serve(daemon: Daemon, pairing: Pairing, token: str) -> None:
    """The channel and the handlers share the language, which lo9i sets on the stream."""
    t = Language()
    app = build_application(token, Handlers(daemon, pairing, t))
    try:
        await app.initialize()
    except InvalidToken:
        logger.error("Telegram refused the bot token.")
        await Runner(daemon, TelegramChannel(daemon, pairing, None, t), LIMITS).run()
        return
    async with polling(app):
        logger.info("Telegram bot running as @%s.", app.bot.username)
        await Runner(daemon, TelegramChannel(daemon, pairing, app.bot, t), LIMITS).run()


if __name__ == "__main__":
    main()
