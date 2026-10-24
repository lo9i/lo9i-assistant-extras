"""vCards (RFC 6350, and the 3.0 that iCloud still serves) as contacts: the parts find_contact gives back, read
with the labels Apple's and Google's clients write (`TYPE`, and `X-ABLabel` on an `itemN.` group)."""

import re

from lo9i_contacts.contacts import Contact, Labeled

# The TYPE values that name a label, in the order one is picked when there are several.
_LABELS = {"cell": "mobile", "iphone": "iPhone", "home": "home", "work": "work", "main": "main", "fax": "fax"}
_APPLE_LABEL = re.compile(r"^_\$!<(.*)>!\$_$")


def parse(text: str) -> list[Contact]:
    """Every contact in the text, which may hold several vCards."""
    cards: list[list[tuple[str, dict[str, list[str]], str, str]]] = []
    for line in _unfolded(text):
        name, params, value, group = _property(line)
        if name == "BEGIN" and value.upper() == "VCARD":
            cards.append([])
        elif cards and name not in ("BEGIN", "END"):
            cards[-1].append((name, params, value, group))
    return [_contact(card) for card in cards]


def _unfolded(text: str) -> list[str]:
    """Its lines, a line that starts with a space or a tab joined to the one before."""
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        elif raw.strip():
            lines.append(raw)
    return lines


def _property(line: str) -> tuple[str, dict[str, list[str]], str, str]:
    """(name, parameters, value, group) of `group.NAME;PARAM=a,b:value`."""
    head, _, value = line.partition(":")
    name, *params = head.split(";")
    group, _, name = name.rpartition(".")
    found: dict[str, list[str]] = {}
    for param in params:
        key, _, values = param.partition("=")
        found.setdefault(key.upper(), []).extend(v.strip('"').lower() for v in values.split(",") if v)
    return name.upper(), found, value, group.lower()


def _contact(card: list[tuple[str, dict[str, list[str]], str, str]]) -> Contact:
    labels = {group: _apple_label(value) for name, _, value, group in card if name == "X-ABLABEL" and group}
    values = {name: value for name, _, value, _ in card}
    name = _text(values.get("FN", "")) or " ".join(reversed(_parts(values.get("N", ""))[:2])).strip()
    organization = _parts(values.get("ORG", ""))[0] if values.get("ORG") else ""

    def labeled(kind: str, read=_text) -> list[Labeled]:
        return [
            Labeled(labels.get(group) or _label(params), read(value))
            for prop, params, value, group in card
            if prop == kind and value
        ]

    return Contact(
        name or organization,
        organization if name else "",
        labeled("TEL"),
        labeled("EMAIL"),
        _birthday(card),
        labeled("ADR", _address),
    )


def _label(params: dict[str, list[str]]) -> str:
    types = params.get("TYPE", [])
    return next((label for key, label in _LABELS.items() if key in types), "other")


def _apple_label(value: str) -> str:
    """Apple writes its own labels as _$!<Mobile>!$_ and the user's as they typed them."""
    found = _APPLE_LABEL.match(value)
    return found.group(1).lower() if found else _text(value)


def _birthday(card: list[tuple[str, dict[str, list[str]], str, str]]) -> str:
    """YYYY-MM-DD, or --MM-DD without the year; "" when there's none. Apple saves a birthday without its year
    with a made-up one, named in X-APPLE-OMIT-YEAR."""
    found = next(((params, value) for name, params, value, _ in card if name == "BDAY"), None)
    if found is None:
        return ""
    params, value = found
    date = re.sub(r"[T ].*$", "", value.strip())
    no_year = re.fullmatch(r"--(\d{2})-?(\d{2})", date)
    if no_year:
        return f"--{no_year.group(1)}-{no_year.group(2)}"
    full = re.fullmatch(r"(\d{4})-?(\d{2})-?(\d{2})", date)
    if full is None:
        return ""
    year, month, day = full.groups()
    return f"--{month}-{day}" if year in params.get("X-APPLE-OMIT-YEAR", []) else f"{year}-{month}-{day}"


def _address(value: str) -> str:
    """ADR's parts (post box, extended, street, city, region, postal code, country), on one line."""
    return ", ".join(part for part in (" ".join(p.split()) for p in _parts(value)) if part)


def _parts(value: str) -> list[str]:
    """A structured value's parts, split on the semicolons that aren't escaped."""
    return [_text(part) for part in re.split(r"(?<!\\);", value)]


def _text(value: str) -> str:
    return (
        value.replace("\\n", "\n").replace("\\N", "\n").replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\")
    ).strip()
