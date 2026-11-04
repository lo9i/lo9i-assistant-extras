"""A page as the assistant reads it: its text as the browser shows it, in parts of a size it asks for,
and its links."""

import re
from typing import Any

from playwright.async_api import Page

DEFAULT_CHARS = 12_000
MAX_CHARS = 50_000
MAX_LINKS = 300
# The links a person sees: shown, with words (their text, else their label or title), leading to a page.
_LINKS = """(limit) => {
  const seen = new Set();
  const links = [];
  for (const a of document.querySelectorAll('a[href]')) {
    if (!a.getClientRects().length) continue;
    const text = (a.innerText || a.getAttribute('aria-label') || a.title || '').replace(/\\s+/g, ' ').trim();
    const url = a.href;
    if (!text || !/^https?:/.test(url)) continue;
    const key = text + '\\n' + url;
    if (seen.has(key)) continue;
    seen.add(key);
    links.push({text: text.slice(0, 200), url});
    if (links.length >= limit) break;
  }
  return links;
}"""
_BLANK_LINES = re.compile(r"\n\s*\n\s*\n+")
_SPACES = re.compile(r"[ \t\xa0]+")


def tidy(text: str) -> str:
    """The text with runs of spaces and of blank lines made one."""
    lines = (_SPACES.sub(" ", line).strip() for line in text.splitlines())
    return _BLANK_LINES.sub("\n\n", "\n".join(lines)).strip()


def part(text: str, offset: int, max_chars: int) -> dict[str, Any]:
    """The text from `offset`, at most `max_chars` of it, with where the next part starts."""
    max_chars = max(1, min(max_chars, MAX_CHARS))
    offset = max(0, offset)
    shown = text[offset : offset + max_chars]
    end = offset + len(shown)
    answer: dict[str, Any] = {"text": shown, "chars": len(text), "offset": offset}
    if end < len(text):
        answer["next_offset"] = end
    return answer


async def text(page: Page) -> str:
    return tidy(await page.locator("body").inner_text(timeout=10_000))


async def links(page: Page, limit: int = MAX_LINKS) -> list[dict[str, str]]:
    return await page.evaluate(_LINKS, limit)


async def read(page: Page, offset: int = 0, max_chars: int = DEFAULT_CHARS, with_links: bool = False) -> dict:
    """The page's address, title and a part of its text; its links too when asked."""
    answer = {"url": page.url, "title": await page.title(), **part(await text(page), offset, max_chars)}
    if with_links:
        answer["links"] = await links(page)
    return answer
