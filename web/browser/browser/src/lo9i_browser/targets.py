"""Finding what to click or type into from the words the assistant read on the page, or a CSS selector."""

from playwright.async_api import Locator, Page


class NotFoundError(Exception):
    pass


def _clickable(page: Page, text: str, exact: bool) -> list[Locator]:
    return [
        page.get_by_role("button", name=text, exact=exact),
        page.get_by_role("link", name=text, exact=exact),
        page.get_by_role("menuitem", name=text, exact=exact),
        page.get_by_role("tab", name=text, exact=exact),
        page.get_by_text(text, exact=exact),
    ]


def _fields(page: Page, name: str, exact: bool) -> list[Locator]:
    return [
        page.get_by_label(name, exact=exact),
        page.get_by_placeholder(name, exact=exact),
        *(page.get_by_role(role, name=name, exact=exact) for role in ("searchbox", "textbox", "combobox")),
    ]


async def _first_shown(candidates: list[Locator]) -> Locator | None:
    for candidate in candidates:
        shown = candidate.filter(visible=True)
        if await shown.count():
            return shown
    return None


async def _find(page: Page, words: str | None, selector: str | None, kind: str) -> Locator:
    if selector:
        found = page.locator(selector).filter(visible=True)
        if await found.count():
            return found
        raise NotFoundError(f"Nothing shown on the page matches the selector {selector!r}.")
    if not words:
        raise NotFoundError(f"Say which {kind}: its words on the page, or a CSS selector.")
    candidates = _clickable if kind == "element" else _fields
    for exact in (True, False):
        found = await _first_shown(candidates(page, words, exact))
        if found is not None:
            return found
    raise NotFoundError(
        f"No {kind} shown on the page has the words {words!r}. Read the page again to see what's there."
    )


async def _nth(found: Locator, index: int) -> Locator:
    count = await found.count()
    if not 0 <= index < count:
        raise NotFoundError(f"{count} shown on the page match; index {index} is none of them (0 is the first).")
    return found.nth(index)


async def clickable(page: Page, text: str | None, selector: str | None, index: int = 0) -> Locator:
    """The `index`th element shown with these words (a button, a link, then any text), or matching the selector."""
    return await _nth(await _find(page, text, selector, "element"), index)


async def field(page: Page, name: str | None, selector: str | None, index: int = 0) -> Locator:
    """The `index`th field shown with this label, placeholder or name, or matching the selector."""
    return await _nth(await _find(page, name, selector, "field"), index)
