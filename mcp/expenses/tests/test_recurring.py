from datetime import date

from expenses import recurring, repo, service
from expenses.recurring import due_date_in, generate


def day(s: str) -> date:
    return date.fromisoformat(s)


def obligation(conn, name, due_day, fixed=None, *, today="2026-01-01", **kw):
    return service.add_obligation(
        conn,
        name,
        "utilities",
        asset_id="h",
        due_day=due_day,
        expected_amount=fixed,
        today=day(today),
        **kw,
    ).id


def confirmed(conn, ob_id, period, amount, due_date):
    service.add_bill(conn, amount, due_date, obligation_id=ob_id, period=period)


def test_due_day_clamps_to_month_end():
    assert due_date_in(2026, 2, 31) == day("2026-02-28")
    assert due_date_in(2028, 2, 30) == day("2028-02-29")
    assert due_date_in(2026, 4, 31) == day("2026-04-30")
    assert due_date_in(2026, 1, 10) == day("2026-01-10")


def test_generates_only_the_current_month_skipping_past_dues(conn):
    # Created in January with due days already past, so nothing yet.
    luz = obligation(conn, "Luz", 10, today="2026-01-31")
    rent = obligation(conn, "Alquiler", 30, 500_000.0, today="2026-01-31")

    # A confirmed September bill for Luz, entered before the schedule ran.
    confirmed(conn, luz, "2026-09", 61_000.0, "2026-09-09")

    # September: Luz already has its bill, rent (due the 30th) is new.
    assert generate(conn, day("2026-09-27")) == 1
    assert repo.find_period_bill(conn, luz, "2026-10") is None
    sep_rent = repo.find_period_bill(conn, rent, "2026-09")
    assert sep_rent.amount == 500_000.0
    assert not sep_rent.estimated

    # Running again creates nothing new.
    assert generate(conn, day("2026-09-28")) == 0

    # October 1: both obligations get October's bill.
    assert generate(conn, day("2026-10-01")) == 2
    oct_luz = repo.find_period_bill(conn, luz, "2026-10")
    assert oct_luz.due_date == "2026-10-10"
    assert oct_luz.amount == 61_000.0
    assert oct_luz.estimated


def test_estimates_follow_the_latest_confirmed_amount(conn):
    # No history: October is created at 0, estimated.
    gas = obligation(conn, "Gas", 5, today="2026-10-01")
    oct = repo.find_period_bill(conn, gas, "2026-10")
    assert (oct.amount, oct.estimated) == (0.0, True)

    # The real September bill shows up late; October's estimate follows.
    confirmed(conn, gas, "2026-09", 18_500.0, "2026-09-05")
    generate(conn, day("2026-10-02"))
    oct = repo.find_period_bill(conn, gas, "2026-10")
    assert (oct.amount, oct.estimated) == (18_500.0, True)


def test_bimonthly_follows_its_anchor(conn):
    # Patente: odd months (Jan, Mar, May...).
    patente = obligation(conn, "Patente", 15, today="2026-01-20", every_months=2, anchor_month=1)

    assert generate(conn, day("2026-02-01")) == 0
    assert generate(conn, day("2026-03-01")) == 1
    assert generate(conn, day("2026-04-01")) == 0
    assert generate(conn, day("2026-05-01")) == 1
    periods = [b.period for b in repo.list_bills(conn)]
    assert periods == ["2026-03", "2026-05"]
    assert repo.find_period_bill(conn, patente, "2026-05").due_date == "2026-05-15"


def test_annual_only_in_its_month(conn):
    obligation(conn, "Ganancias", 20, today="2026-01-01", every_months=12, anchor_month=6)
    created = [generate(conn, date(2026, m, 1)) for m in range(1, 13)]
    assert created == [0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0]


def test_on_schedule_wraps_the_year():
    # Quarterly from November: Nov, Feb, May, Aug.
    months = [m for m in range(1, 13) if recurring.on_schedule(m, 3, 11)]
    assert months == [2, 5, 8, 11]
