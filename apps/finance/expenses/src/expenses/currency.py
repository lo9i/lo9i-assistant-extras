"""The one currency everything is in, and how amounts are written.

The first time it's needed, it's the one of the country the user lives in, found from the time zone
lo9i runs the plugin with (TZ), or from the browser's region when the zone names no country. It's
kept in the settings table; the user changes it on the settings page or through set_currency.
Changing it relabels every amount: nothing is converted.
"""

import os
import sqlite3
from dataclasses import dataclass
from decimal import Decimal
from functools import cache
from pathlib import Path

from babel import Locale, UnknownLocaleError, localedata
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


@dataclass(frozen=True)
class Format:
    """One way of writing amounts in a currency, for the settings page's list."""

    locale: str
    # 45230.5 written this way, like "$ 45.230,50".
    example: str
    # The locale's name in English, like "Spanish (Argentina)".
    name: str


EXAMPLE_AMOUNT = 45230.5


def formats(code: str, keep: str | None = None) -> list[Format]:
    """How amounts in `code` are written where it's used, one entry per distinct way, each under one locale
    that writes it so: `keep` (the format chosen before, listed even where the currency isn't used), else the
    currency's usual one (locale_for), else a region's main language. The usual one comes first."""
    usual = locale_for(code)
    groups: dict[str, list[str]] = {}
    for locale in dict.fromkeys([*filter(None, [keep]), usual, *_locales_using(code)]):
        groups.setdefault(Currency(code, locale).format(EXAMPLE_AMOUNT), []).append(locale)
    found = [_format(code, _representative(locales, keep, usual)) for locales in groups.values()]
    return sorted(found, key=lambda f: (f.example != Currency(code, usual).format(EXAMPLE_AMOUNT), f.name))


def get(conn: sqlite3.Connection) -> Currency | None:
    rows = dict(conn.execute("SELECT key, value FROM settings WHERE key IN ('currency', 'locale')").fetchall())
    return Currency(rows["currency"], rows["locale"]) if "currency" in rows and "locale" in rows else None


def set(conn: sqlite3.Connection, code: str, locale: str | None = None) -> Currency:
    """Raises ValidationError for an unknown currency or locale. Without a locale, the current one is
    kept, or the one most used with the currency."""
    code = code.strip().upper()
    if not known(code):
        raise ValidationError(f"{code} isn't an ISO 4217 currency code, like ARS, USD or EUR")
    current = get(conn)
    chosen = Currency(code, _locale(locale) if locale else (current.locale if current else locale_for(code)))
    with conn:
        conn.executemany(
            "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            [("currency", chosen.code), ("locale", chosen.locale)],
        )
    return chosen


def known(code: str) -> bool:
    """An ISO 4217 code, like ARS or USD."""
    return code in list_currencies()


def ensure(conn: sqlite3.Connection, fallback: Currency | None = None) -> Currency | None:
    """The chosen currency. When none is chosen yet, the local one (or `fallback`) is saved and
    returned, so it stays the same if the time zone changes later. None when neither is known."""
    if chosen := get(conn):
        return chosen
    found = local() or fallback
    return set(conn, found.code, found.locale) if found else None


def local(zone: str | None = None) -> Currency | None:
    """The currency of the country the time zone is in (America/Argentina/Buenos_Aires → ARS in
    es_AR). Without a zone, the one in TZ, else the system's. None for zones in no country (UTC)."""
    zone = zone or _system_zone()
    zone = get_global("zone_aliases").get(zone, zone)
    territory = get_global("zone_territories").get(zone)
    codes = get_territory_currencies(territory) if territory else []
    if not codes:
        return None
    return Currency(codes[0], _territory_locale(territory) or locale_for(codes[0]))


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
        if found := _territory_locale(territory):
            return found
    return "en_US"


@cache
def _locales_using(code: str) -> list[str]:
    """Every locale of a region where the currency is legal tender now (EUR → de_DE, fr_FR, nl_BE…)."""
    users = {t for t, history in get_global("territory_currencies").items() if _uses(history, code)}
    return [locale for territory, locales in _locales_by_territory().items() if territory in users for locale in locales]


@cache
def _locales_by_territory() -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for identifier in localedata.locale_identifiers():
        locale = Locale.parse(identifier)
        # Variants (en_US_POSIX, ca_ES_VALENCIA) aren't number formats people pick.
        if locale.territory and not locale.variant:
            found.setdefault(locale.territory, []).append(identifier)
    return found


def _representative(locales: list[str], keep: str | None, usual: str) -> str:
    """The locale a way of writing is listed under: the chosen one, the usual one, else the main language
    of a region that writes it so, else the first."""
    for preferred in (keep, usual):
        if preferred in locales:
            return preferred
    main = [loc for loc in locales if _territory_locale(Locale.parse(loc).territory or "") == loc]
    return min(main) if main else locales[0]


def _format(code: str, locale: str) -> Format:
    return Format(locale, Currency(code, locale).format(EXAMPLE_AMOUNT), Locale.parse(locale).get_display_name("en"))


def _territory_locale(territory: str) -> str | None:
    """How numbers are written in a region: its most used language there (AR → es_AR)."""
    # No entry means the default language (und → en_Latn_US), as for the US itself.
    subtags = get_global("likely_subtags")
    language = subtags.get(f"und_{territory}", subtags["und"]).split("_")[0]
    return f"{language}_{territory}" if language and _exists(f"{language}_{territory}") else None


def _system_zone() -> str:
    """TZ as lo9i sets it, else the zone /etc/localtime links to (…/zoneinfo/Europe/Madrid)."""
    if zone := os.environ.get("TZ", "").removeprefix(":"):
        return zone.split("zoneinfo/")[-1]
    target = str(Path("/etc/localtime").resolve())
    return target.split("zoneinfo/")[-1] if "zoneinfo/" in target else ""


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
