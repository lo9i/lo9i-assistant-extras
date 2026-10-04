"""What the apps show for Telegram: the bot, the paired accounts, the open pairing code and a button
for a new one."""

from lo9i_chat import status
from lo9i_chat.status import SetupItem
from lo9i_telegram.pairing import Pairing

NEW_PAIRING_CODE = "new-pairing-code"


def items(bot_line: str, pairing: Pairing) -> list[SetupItem]:
    """`bot_line` is "Bot: @username", or why the bot isn't running."""
    return [
        status.text(bot_line),
        status.text(_paired(pairing)),
        *_code(pairing),
        status.action("New pairing code", NEW_PAIRING_CODE),
    ]


def _paired(pairing: Pairing) -> str:
    names = [u.name for u in pairing.users]
    return f"Paired: {', '.join(names)}" if names else "Nobody is paired yet."


def _code(pairing: Pairing) -> list[SetupItem]:
    if not pairing.code:
        return []
    minutes = int(pairing.lifetime.total_seconds() // 60)
    return [
        status.code("Pairing code", pairing.code),
        status.text(f"Send this code to the bot in Telegram within {minutes} minutes."),
    ]
