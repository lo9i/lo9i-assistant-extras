"""The invoices ready to issue, for lo9i's list of the user's day (`today:` in server.yaml; lo9i's docs/today-lists.md):
those of the halves of a month that ended (1st-15th, 16th-end) since the start of the month before, not issued yet,
each saying it needs issuing, which lo9i keeps as an open loop until it's issued and leaves the list. Nothing while the
setup misses something: there's nothing to issue with yet. One that can't be prepared says why, with nothing to do."""

import sqlite3
from datetime import date, timedelta
from typing import Any

from . import service

_ISSUED = "authorized"
_CANT = "can't prepare"
_TEXTS = {
    "en": {
        "heading": "Invoices to issue",
        "issue": "Issue {customer}'s invoice for {period}",
        "months": "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec",
    },
    "es": {
        "heading": "Facturas por emitir",
        "issue": "Emitir la factura de {customer} del {period}",
        "months": "ene feb mar abr may jun jul ago sep oct nov dic",
    },
}


def listed(conn: sqlite3.Connection, day: date, language: str) -> dict[str, Any]:
    texts = _TEXTS.get(language, _TEXTS["en"])
    items = [_item(entry, texts, language) for month in _months(day) for entry in _ended(conn, month, day)]
    return {"heading": texts["heading"], "items": items, "note": ""}


def _months(day: date) -> list[str]:
    """The month before and this one, YYYY-MM."""
    before = day.replace(day=1) - timedelta(days=1)
    return [before.strftime("%Y-%m"), day.strftime("%Y-%m")]


def _ended(conn: sqlite3.Connection, month: str, day: date) -> list[dict[str, Any]]:
    """The month's invoices whose period ended before `day` and that aren't issued; none while the setup misses
    something."""
    prepared = service.prepare_month(conn, month=month, on=day)
    if prepared["missing_setup"]:
        return []
    return [e for e in prepared["invoices"] if e["status"] != _ISSUED and date.fromisoformat(e["period_to"]) < day]


def _item(entry: dict[str, Any], texts: dict[str, str], language: str) -> dict[str, Any]:
    customer = entry["customer"]["name"]
    period = _period(entry, texts)
    if entry["status"] == _CANT:
        return {"title": customer, "subtitle": f"{period} · {entry['error']}", "kind": "invoices to issue"}
    return {
        "title": customer,
        "subtitle": f"{period} · {_pesos(entry['total'], language)}",
        "needs": texts["issue"].format(customer=customer, period=period),
        "kind": "invoices to issue",
        "key": f"billing {entry['customer']['id']} {entry['period_from']}",
    }


def _period(entry: dict[str, Any], texts: dict[str, str]) -> str:
    """1-15 Oct."""
    start, end = date.fromisoformat(entry["period_from"]), date.fromisoformat(entry["period_to"])
    return f"{start.day}-{end.day} {texts['months'].split()[end.month - 1]}"


def _pesos(amount: float, language: str) -> str:
    """$ 302,500.00, or $ 302.500,00 in Spanish."""
    written = f"{amount:,.2f}"
    if language == "es":
        written = written.replace(",", "_").replace(".", ",").replace("_", ".")
    return f"$ {written}"
