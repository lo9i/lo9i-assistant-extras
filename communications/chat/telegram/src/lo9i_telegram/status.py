"""What the apps show for Telegram: the bot, the paired accounts, the open pairing code and a button
for a new one. Each item is `kind` (text, code, copy or action), `label` and `value`."""

from lo9i_telegram.pairing import Pairing

NEW_PAIRING_CODE = "new-pairing-code"


def items(bot_line: str, pairing: Pairing) -> list[dict[str, str]]:
    """`bot_line` is "Bot: @username", or why the bot isn't running."""
    return [
        _item("text", "", bot_line),
        _item("text", "", _paired(pairing)),
        *_code(pairing),
        _item("action", "New pairing code", NEW_PAIRING_CODE),
    ]


def _paired(pairing: Pairing) -> str:
    names = [u.name for u in pairing.users]
    return f"Paired: {', '.join(names)}" if names else "Nobody is paired yet."


def _code(pairing: Pairing) -> list[dict[str, str]]:
    if not pairing.code:
        return []
    minutes = int(pairing.lifetime.total_seconds() // 60)
    return [
        _item("code", "Pairing code", pairing.code),
        _item("text", "", f"Send this code to the bot in Telegram within {minutes} minutes."),
    ]


def _item(kind: str, label: str, value: str) -> dict[str, str]:
    return {"kind": kind, "label": label, "value": value}
