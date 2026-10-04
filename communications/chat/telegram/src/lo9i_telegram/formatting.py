"""Markdown for Telegram, and its message size."""

import telegramify_markdown

# Below Telegram's 4096-character limit, leaving room for MarkdownV2 escaping.
CHUNK = 3500


def to_markdown_v2(text: str) -> str:
    return telegramify_markdown.markdownify(text)
