"""The plugin's own text in the language lo9i gives: every text the code marks has a Spanish translation and the
table holds nothing the code no longer marks; the status and the replies follow a change at once."""

import ast
import json
from pathlib import Path
from string import Formatter
from unittest.mock import AsyncMock, MagicMock, create_autospec

from telegram import Message, Update

import lo9i_telegram
from lo9i_telegram.channel import TelegramChannel
from lo9i_telegram.handlers import Handlers
from lo9i_telegram.i18n import Language
from lo9i_telegram.pairing import Pairing

_PACKAGE = Path(lo9i_telegram.__file__).parent
_SPANISH = json.loads((_PACKAGE / "locales" / "es.json").read_text(encoding="utf-8"))


def test_every_marked_text_has_a_spanish_translation_and_none_is_left_over():
    marked = {text for path in sorted(_PACKAGE.rglob("*.py")) for text in _marked(path)}
    assert sorted(marked - _SPANISH.keys()) == []
    assert sorted(_SPANISH.keys() - marked) == []


def test_translations_keep_their_placeholders():
    assert [english for english, spanish in _SPANISH.items() if _fields(english) != _fields(spanish)] == []


def test_a_language_without_a_table_is_english():
    assert Language("fr")("Pairing code") == "Pairing code"
    assert Language("es")("Paired: {names}", names="Ani") == "Vinculados: Ani"


class _Status:
    def __init__(self):
        self.items = []

    async def set_status(self, items):
        self.items = [i["value"] if i["kind"] == "text" else i["label"] for i in items]


async def test_the_status_follows_the_language_at_once(tmp_path, bot):
    status, pairing = _Status(), await Pairing.load(tmp_path)
    channel = TelegramChannel(status, pairing, bot, Language())
    await channel.hello("Max", [])
    assert "Nobody is paired yet." in status.items
    await channel.language("es")
    assert status.items[:3] == ["Bot: @lo9i_bot", "Todavía no hay nadie vinculado.", "Código de vinculación"]
    pairing.close()


async def test_the_pairing_reply_is_in_the_language(tmp_path):
    pairing = await Pairing.load(tmp_path)
    message = create_autospec(Message, instance=True, text=pairing.code)
    message.reply_text = AsyncMock()
    update = MagicMock(spec=Update, effective_message=message)
    update.effective_user.id, update.effective_user.username = 222, "ani"
    await Handlers(MagicMock(), pairing, Language("es")).message(update, MagicMock())
    assert message.reply_text.call_args.args[0].startswith("Vinculado ✓.")
    pairing.close()


def _marked(path: Path) -> list[str]:
    return [
        node.args[0].value
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.Call)
        and _name(node.func) in {"t", "_t", "translatable"}
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    ]


def _name(func: ast.expr) -> str:
    if isinstance(func, ast.Name):
        return func.id
    return func.attr if isinstance(func, ast.Attribute) else ""


def _fields(text: str) -> set[str]:
    return {name for _, name, _, _ in Formatter().parse(text) if name}
