"""The bills coming due, for lo9i's list of the user's day (`today:` in server.yaml; lo9i's docs/today-lists.md): the
unpaid ones overdue or due within WEEK days, the oldest due first. One due within SOON days, or overdue, says it
needs paying, which lo9i keeps as an open loop until the bill is paid and leaves the list."""

import sqlite3
from datetime import date, timedelta
from typing import Any

from . import currency, service
from .models import Bill

WEEK = 7
SOON = 3
_TEXTS = {
    "en": {
        "heading": "Bills",
        "none": "No bills due this week.",
        "overdue": "overdue",
        "pay": "Pay {name} ({amount}), due {due}",
        "pay_overdue": "Pay {name} ({amount}), overdue since {due}",
        "day": "{month} {day}",
        "months": "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec",
    },
    "es": {
        "heading": "Facturas",
        "none": "No vencen facturas esta semana.",
        "overdue": "vencida",
        "pay": "Pagar {name} ({amount}), vence el {due}",
        "pay_overdue": "Pagar {name} ({amount}), vencida desde el {due}",
        "day": "{day} de {month}",
        "months": "enero febrero marzo abril mayo junio julio agosto septiembre octubre noviembre diciembre",
    },
}


def listed(conn: sqlite3.Connection, day: date, language: str) -> dict[str, Any]:
    texts = _TEXTS.get(language, _TEXTS["en"])
    chosen = currency.ensure(conn)
    assets = {a.id: a.name for a in service.list_assets(conn)}
    last = (day + timedelta(days=WEEK)).isoformat()
    due = sorted(
        (b for b in service.list_bills(conn, status="pending") if b.due_date <= last),
        key=_due,
    )
    items = [_item(b, day, texts, chosen, assets) for b in due]
    return {
        "heading": texts["heading"],
        "items": items,
        "note": "" if items else texts["none"],
    }


def _item(
    bill: Bill,
    day: date,
    texts: dict[str, str],
    chosen: currency.Currency | None,
    assets: dict[str, str],
) -> dict[str, Any]:
    name = bill.provider or bill.category
    amount = chosen.format(bill.amount) if chosen else f"{bill.amount:,.2f}"
    overdue = bill.due_date < day.isoformat()
    subtitle = " · ".join(
        filter(
            None,
            [
                amount,
                assets.get(bill.asset_id or "", ""),
                texts["overdue"] if overdue else "",
            ],
        )
    )
    return {
        "title": name,
        "subtitle": subtitle,
        "date": bill.due_date,
        "page_path": f"bills/{bill.id}",
        "needs": _needs(bill, day, texts, name, amount),
        "kind": "bill due",
        "key": f"expenses bill {bill.id}",
    }


def _needs(bill: Bill, day: date, texts: dict[str, str], name: str, amount: str) -> str:
    """Only for one overdue or due within SOON days."""
    if bill.due_date > (day + timedelta(days=SOON)).isoformat():
        return ""
    template = texts["pay_overdue"] if bill.due_date < day.isoformat() else texts["pay"]
    return template.format(name=name, amount=amount, due=_day(date.fromisoformat(bill.due_date), texts))


def _day(when: date, texts: dict[str, str]) -> str:
    """Nov 5, or 5 de noviembre."""
    return texts["day"].format(month=texts["months"].split()[when.month - 1], day=when.day)


def _due(bill: Bill) -> str:
    return bill.due_date
