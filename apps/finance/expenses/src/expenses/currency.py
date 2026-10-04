"""The one currency everything is in, and how amounts are written.

Chosen by the user (the web page asks on the first visit, guessing from the browser's language and
region; the assistant can set it with set_currency) and kept in the settings table. Changing it
relabels every amount: nothing is converted.
"""

import sqlite3
from dataclasses import dataclass
from decimal import Decimal

from babel import Locale, UnknownLocaleError
from babel.core import get_global
from babel.numbers import (
    NumberFormatError,
    format_currency,
    format_decimal,
    get_territory_currencies,
    list_currencies,
    parse_decimal,
)

from .service import ValidationError


@dataclass(frozen=True)
class Currency:
    # ISO 4217, like ARS or USD.
    code: str
    # How numbers are written, like es_AR (45.230,50) or en_US (45,230.50).
    locale: str

    def format(self, amount: float) -> str:
        """With the currency's symbol: "$ 45.230,50" for ARS in es_AR."""
        return format_currency(amount, self.code, locale=self.locale)

    def plain(self, amount: float) -> str:
        """For an input field: no symbol or grouping, "45230,5" in es_AR."""
        return format_decimal(amount, format="0.##", locale=self.locale)

    def parse(self, text: str) -> float:
        """An amount as typed, written the way the locale writes numbers. Raises ValueError."""
        try:
            return float(parse_decimal(text.strip(), locale=self.locale, strict=True))
        except NumberFormatError:
            raise ValueError(f"write it like {self.plain(45230.5)} or {format_decimal(Decimal('45230.5'), locale=self.locale)}") from None


def get(conn: sqlite3.Connection) -> Currency | None:
    rows = dict(conn.execute("SELECT key, value FROM settings WHERE key IN ('currency', 'locale')").fetchall())
    return Currency(rows["currency"], rows["locale"]) if "currency" in rows and "locale" in rows else None


def set(conn: sqlite3.Connection, code: str, locale: str | None = None) -> Currency:
    """Raises ValidationError for an unknown currency or locale. Without a locale, the current one is
    kept, or the one most used with the currency."""
    code = code.strip().upper()
    if code not in list_currencies():
        raise ValidationError(f"{code} isn't an ISO 4217 currency code, like ARS, USD or EUR")
    current = get(conn)
    chosen = Currency(code, _locale(locale) if locale else (current.locale if current else locale_for(code)))
    with conn:
        conn.executemany(
            "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            [("currency", chosen.code), ("locale", chosen.locale)],
        )
    return chosen


def guess(accept_language: str) -> Currency | None:
    """The currency of the region in a browser's Accept-Language (es-AR → ARS in es_AR), or None
    when it names no region."""
    for tag in (part.split(";")[0].strip() for part in accept_language.split(",")):
        try:
            locale = Locale.parse(tag, sep="-")
        except (ValueError, UnknownLocaleError):
            continue
        codes = get_territory_currencies(locale.territory) if locale.territory else []
        if codes:
            return Currency(codes[0], str(locale))
    return None


def choices(locale: str) -> list[tuple[str, str]]:
    """Every currency (code, name), named in `locale`, by name."""
    names = Locale.parse(locale).currencies
    return sorted(((c, names.get(c, c)) for c in list_currencies()), key=lambda item: item[1].lower())


def locale_for(code: str) -> str:
    """How numbers are written where the currency is used: the region named by the code's first two
    letters (ARS → AR → es_AR), else the first region that uses it (EUR → de_AT), else en_US."""
    users = [t for t, history in get_global("territory_currencies").items() if _uses(history, code)]
    for territory in sorted(users, key=lambda t: t != code[:2]):
        # No entry means the default language (und → en_Latn_US), as for the US itself.
        subtags = get_global("likely_subtags")
        language = subtags.get(f"und_{territory}", subtags["und"]).split("_")[0]
        if language and _exists(f"{language}_{territory}"):
            return f"{language}_{territory}"
    return "en_US"


def _uses(history: tuple, code: str) -> bool:
    """Whether a region's currency history has `code` as a current legal tender."""
    return any(entry[0] == code and entry[2] is None and entry[3] for entry in history)


def _exists(locale: str) -> bool:
    try:
        Locale.parse(locale)
    except (ValueError, UnknownLocaleError):
        return False
    return True


def _locale(tag: str) -> str:
    try:
        return str(Locale.parse(tag.strip().replace("-", "_")))
    except (ValueError, UnknownLocaleError):
        raise ValidationError(f"{tag} isn't a locale, like es_AR or en_US") from None
