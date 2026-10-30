"""What the apps show for Telegram, in the language the user chose: the bot, the paired accounts, the open
pairing code and a button for a new one. Each item is `kind` (text, code, copy or action), `label` and `value`."""

from lo9i_telegram.i18n import Language, translatable
from lo9i_telegram.pairing import Pairing

NEW_PAIRING_CODE = "new-pairing-code"
# Never the library's text: it quotes the token, and the status reaches every app.
REFUSED = translatable("Telegram refused the bot token. Check it with @BotFather and enter it again.")


def items(t: Language, bot_line: str, pairing: Pairing) -> list[dict[str, str]]:
    """`bot_line` is "Bot: @username", or why the bot isn't running."""
    return [
        _item("text", "", bot_line),
        _item("text", "", _paired(t, pairing)),
        *_code(t, pairing),
        _item("action", t("New pairing code"), NEW_PAIRING_CODE),
    ]


def _paired(t: Language, pairing: Pairing) -> str:
    names = [u.name for u in pairing.users]
    return t("Paired: {names}", names=", ".join(names)) if names else t("Nobody is paired yet.")


def _code(t: Language, pairing: Pairing) -> list[dict[str, str]]:
    if not pairing.code:
        return []
    minutes = int(pairing.lifetime.total_seconds() // 60)
    return [
        _item("code", t("Pairing code"), pairing.code),
        _item("text", "", t("Send this code to the bot in Telegram within {minutes} minutes.", minutes=minutes)),
    ]


def _item(kind: str, label: str, value: str) -> dict[str, str]:
    return {"kind": kind, "label": label, "value": value}
