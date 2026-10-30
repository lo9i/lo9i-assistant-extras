"""The plugin's own text in the language lo9i gives: every text the code marks has a Spanish translation and the
table holds nothing the code no longer marks; what the handlers say follows the language."""

import ast
import json
from pathlib import Path
from string import Formatter

import lo9i_slack
from lo9i_slack.handlers import Handlers
from lo9i_slack.i18n import Language

_PACKAGE = Path(lo9i_slack.__file__).parent
_SPANISH = json.loads((_PACKAGE / "locales" / "es.json").read_text(encoding="utf-8"))


def test_every_marked_text_has_a_spanish_translation_and_none_is_left_over():
    marked = {text for path in sorted(_PACKAGE.rglob("*.py")) for text in _marked(path)}
    assert sorted(marked - _SPANISH.keys()) == []
    assert sorted(_SPANISH.keys() - marked) == []


def test_translations_keep_their_placeholders():
    assert [english for english, spanish in _SPANISH.items() if _fields(english) != _fields(spanish)] == []


async def test_someone_else_is_told_in_the_language():
    handlers = Handlers(object(), object(), "xoxb-1", "U1", Language("es"))  # type: ignore[arg-type]
    answer = await handlers.command({"user_id": "U2", "command": "/clear"})
    assert answer == {"text": "Este asistente solo responde a la persona que lo configuró."}


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
