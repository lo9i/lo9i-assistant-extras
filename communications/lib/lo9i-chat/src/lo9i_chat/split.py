"""Splitting long replies to fit a channel's message limit."""


def split_text(text: str, limit: int) -> list[str]:
    """Split on paragraph breaks, then line breaks, then hard cuts, keeping each part within `limit`."""
    chunks: list[str] = []
    rest = text
    while len(rest) > limit:
        cut = _cut_point(rest, limit)
        chunks.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip("\n")
    return [*chunks, rest] if rest else chunks


def _cut_point(text: str, limit: int) -> int:
    for separator in ("\n\n", "\n", " "):
        cut = text.rfind(separator, 0, limit)
        if cut > limit // 2:
            return cut
    return limit
