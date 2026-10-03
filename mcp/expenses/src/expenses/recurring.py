"""Bills for obligations with a `due_day`. Each scheduled month's bill is
created ahead of time as pending, and the real bill later updates it in place
(see `service.add_bill`)."""

import calendar
import sqlite3
from datetime import date

from . import repo


def period_of(d: date) -> str:
    """`YYYY-MM` for a date."""
    return d.strftime("%Y-%m")


def due_date_in(year: int, month: int, due_day: int) -> date:
    """`due_day` in the given month, pulled back to the month's last day when
    the month is shorter (31 -> 30 in April)."""
    last = calendar.monthrange(year, month)[1]
    return date(year, month, min(max(due_day, 1), last))


def on_schedule(month: int, every_months: int, anchor_month: int | None) -> bool:
    """Whether an obligation repeating every `every_months` from
    `anchor_month` has a bill in `month`."""
    if every_months == 1:
        return True
    return (month - (anchor_month or 1)) % every_months == 0


def generate(conn: sqlite3.Connection, today: date | None = None) -> int:
    """Create this month's missing bills, then refresh estimates. Returns how
    many bills were created."""
    with conn:
        return _generate(conn, today or date.today())


def _generate(conn: sqlite3.Connection, today: date) -> int:
    """`generate` without its own transaction, for callers already in one.

    A bill whose due date has already passed is not created: if it was real,
    it would have been entered by now."""
    created = 0
    for ob in repo.list_obligations(conn):
        if ob.due_day is None or not on_schedule(today.month, ob.every_months, ob.anchor_month):
            continue
        due = due_date_in(today.year, today.month, ob.due_day)
        if due < today:
            continue
        period = period_of(due)
        if repo.find_period_bill(conn, ob.id, period):
            continue
        if ob.expected_amount is not None:
            amount, estimated = ob.expected_amount, False
        else:
            amount, estimated = repo.last_confirmed_amount(conn, ob.id, period) or 0.0, True
        repo.insert_bill(
            conn,
            obligation_id=ob.id,
            period=period,
            estimated=estimated,
            amount=amount,
            due_date=due.isoformat(),
        )
        created += 1

    _refresh_estimates(conn)
    return created


def _refresh_estimates(conn: sqlite3.Connection) -> None:
    """Pending estimated bills take the latest confirmed amount before their
    period, so a real bill entered late for last month still feeds this
    month's estimate."""
    for b in repo.list_bills(conn, status="pending"):
        if b.obligation_id is None or not b.estimated:
            continue
        amount = repo.last_confirmed_amount(conn, b.obligation_id, b.period)
        if amount is not None and amount != b.amount:
            repo.update_bill(conn, b.id, {"amount": amount})
