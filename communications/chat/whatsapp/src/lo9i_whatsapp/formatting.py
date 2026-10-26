"""Markdown for WhatsApp, which has its own marks: *bold*, _italics_, ~strikethrough~, `code` and ```blocks```.
Code is left as it is; headings become bold lines and links show their address."""

import re

# Below WhatsApp's limit (65,536 characters), so a long reply comes in parts a phone can read.
CHUNK = 3500

_CODE = re.compile(r"```[^\n`]*\n?.*?```|`[^`\n]+`", re.DOTALL)
_FENCE = re.compile(r"```[^\n`]*\n?(.*?)```", re.DOTALL)
_BOLD = re.compile(r"\*\*(.+?)\*\*|__(.+?)__", re.DOTALL)
_ITALIC = re.compile(r"(?<![\w*])\*(?![\s*])(.+?)(?<![\s*])\*(?![\w*])")
_STRIKE = re.compile(r"~~(.+?)~~", re.DOTALL)
_HEADING = re.compile(r"^#{1,6}[ \t]+(.+?)[ \t#]*$", re.MULTILINE)
_BULLET = re.compile(r"^([ \t]*)[*+][ \t]+", re.MULTILINE)
_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
# Stands for a bold mark while italics are rewritten, so they aren't taken for one.
_STAR = "\x00"


def to_whatsapp(markdown: str) -> str:
    parts, last = [], 0
    for code in _CODE.finditer(markdown):
        parts.append(_prose(markdown[last : code.start()]))
        parts.append(_FENCE.sub(r"```\1```", code.group()))
        last = code.end()
    parts.append(_prose(markdown[last:]))
    return "".join(parts)


def _prose(text: str) -> str:
    text = _HEADING.sub(lambda m: f"{_STAR}{m.group(1).replace('**', '')}{_STAR}", text)
    text = _BULLET.sub(r"\1- ", text)
    text = _BOLD.sub(lambda m: f"{_STAR}{m.group(1) or m.group(2)}{_STAR}", text)
    text = _ITALIC.sub(r"_\1_", text)
    text = _STRIKE.sub(r"~\1~", text)
    text = _LINK.sub(lambda m: m.group(2) if m.group(1) == m.group(2) else f"{m.group(1)} ({m.group(2)})", text)
    return text.replace(_STAR, "*")
