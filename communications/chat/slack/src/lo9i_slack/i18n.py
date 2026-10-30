"""The plugin's own text (its setup in the apps, its replies in the chat) in the language the user chose for the
apps' text, which lo9i gives on its stream (`hello`, then `language` when it changes; lo9i's
docs/channel-plugins.md). Text is written in English where it's used, inside
`t("...")`, or marked with `translatable("...")` where it's kept before it's shown, and looked up in a table per
language (locales/<code>.json) whose keys are the English: English needs none, and a text a table lacks shows in
English. The tests fail on a text the Spanish table lacks (tests/test_i18n.py)."""

import json
from pathlib import Path

_TABLES = {"es": json.loads((Path(__file__).parent / "locales" / "es.json").read_text(encoding="utf-8"))}
ENGLISH = "en"


def translatable(text: str) -> str:
    """Marks text translated later, where it's shown."""
    return text


class Language:
    """The language the plugin writes in: English until lo9i says another."""

    def __init__(self, code: str = ENGLISH) -> None:
        self.code = ENGLISH
        self._table: dict[str, str] = {}
        self.use(code)

    def use(self, code: str) -> None:
        """A language without a table is English."""
        self.code = code if code in _TABLES else ENGLISH
        self._table = _TABLES.get(self.code, {})

    def __call__(self, text: str, /, **values: object) -> str:
        """`text` in the language, its {placeholders} filled from `values`."""
        translated = self._table.get(text, text)
        return translated.format(**values) if values else translated
