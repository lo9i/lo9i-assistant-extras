"""lo9i's check of the bot token before it's saved (`check:` in server.yaml, lo9i's docs/plugin-checks.md): it asks
Telegram who the bot is (getMe), so a wrong token shows in the install dialog instead of a bot that never answers.
It exits 1 saying why on stdout when Telegram refuses the token. When Telegram can't be reached it lets the token
through: the bot starts once Telegram is back."""

import asyncio
import os
import sys

from telegram import Bot
from telegram.error import InvalidToken, TelegramError
from telegram.request import BaseRequest

from lo9i_telegram.app import REFUSED


async def problem(token: str, request: BaseRequest | None = None) -> str | None:
    """Why the bot can't run with `token`; None when it can, or when that can't be told now. `request` reaches
    Telegram, the library's own when None."""
    try:
        async with Bot(token.strip(), request=request):
            # Starting it asks Telegram who the bot is.
            pass
    except InvalidToken:
        return REFUSED
    except TelegramError:
        return None
    return None


def main() -> None:
    reason = asyncio.run(problem(os.environ.get("TELEGRAM_TOKEN", "")))
    if reason:
        print(reason)
        sys.exit(1)


if __name__ == "__main__":
    main()
