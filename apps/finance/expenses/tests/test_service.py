from datetime import date

import pytest

from expenses import db, repo, service
from expenses.service import Conflict, NotFound, ValidationError

TODAY = date(2026, 10, 1)


def ob(conn, name="Luz", **kw):
    kw.setdefault("asset_id", "h")
    return service.add_obligation(conn, name, kw.pop("category", "utilities"), today=TODAY, **kw)


# --- Assets ---


def test_asset_id_is_a_slug_of_the_name(conn):
    a = service.add_asset(conn, "Casa Peñalolén", "house", metadata={" address ": " Av 1 "})
    assert a.id == "casa-penalolen"
    assert a.metadata == {"address": "Av 1"}


def test_asset_rules(conn):
    with pytest.raises(Conflict):
        service.add_asset(conn, "H", id="h")
    with pytest.raises(ValidationError, match="kind"):
        service.add_asset(conn, "Boat", "boat")
    car = service.add_asset(conn, "Car", "car")
    assert service.update_asset(conn, car.id, notes=" gol ").notes == "gol"


def test_asset_with_bills_cannot_be_removed(conn):
    luz = ob(conn)
    service.add_bill(conn, 100, "2026-09-10", obligation_id=luz.id)
    with pytest.raises(Conflict, match="1 bill"):
        service.remove_asset(conn, "h")


def test_removing_an_asset_takes_its_obligations(conn):
    luz = ob(conn)
    service.remove_asset(conn, "h")
    with pytest.raises(NotFound):
        service.get_obligation(conn, luz.id)


# --- Categories ---


def test_categories_are_created_on_first_use(conn):
    service.add_bill(conn, 50, "2026-10-03", category="  Car   Repair ")
    assert "car repair" in service.list_categories(conn)
    with pytest.raises(Conflict):
        service.remove_category(conn, "car repair")


def test_unused_category_can_be_removed(conn):
    service.add_category(conn, "Gifts")
    service.remove_category(conn, "gifts")
    assert "gifts" not in service.list_categories(conn)


# --- Obligations ---


def test_obligation_without_asset(conn):
    mono = ob(conn, "Monotributo", asset_id=None, category="tax")
    assert mono.asset_id is None
    # Names are unique per asset, and "no asset" counts as one.
    with pytest.raises(Conflict):
        ob(conn, "monotributo", asset_id=None, category="tax")
    ob(conn, "Monotributo", asset_id="h", category="tax")


def test_obligation_errors_list_valid_choices(conn):
    with pytest.raises(ValidationError, match=r"expected one of \['h'\]"):
        ob(conn, asset_id="nope")
    with pytest.raises(ValidationError, match="anchor_month"):
        ob(conn, due_day=10, every_months=2)
    with pytest.raises(ValidationError, match="every_months"):
        ob(conn, due_day=10, every_months=4, anchor_month=1)


def test_obligation_rename_checks_siblings(conn):
    ob(conn, "Luz")
    gas = ob(conn, "Gas")
    with pytest.raises(Conflict):
        service.update_obligation(conn, gas.id, name="LUZ")


def test_obligation_with_due_day_creates_its_bill(conn):
    luz = ob(conn, due_day=10, expected_amount=1000)
    bill = repo.find_period_bill(conn, luz.id, "2026-10")
    assert (bill.amount, bill.estimated, bill.asset_id, bill.category) == (
        1000,
        False,
        "h",
        "utilities",
    )


def test_obligation_with_bills_cannot_be_removed(conn):
    luz = ob(conn)
    service.add_bill(conn, 100, "2026-09-10", obligation_id=luz.id)
    with pytest.raises(Conflict):
        service.remove_obligation(conn, luz.id)


# --- Bills ---


def test_one_off_bill_needs_a_category(conn):
    with pytest.raises(ValidationError, match="category is required"):
        service.add_bill(conn, 10, "2026-10-01")
    b = service.add_bill(conn, 10, "2026-10-01", category="transport", asset_id="h")
    assert (b.asset_id, b.category, b.period) == ("h", "transport", None)


def test_one_off_bill_rejects_period(conn):
    with pytest.raises(ValidationError, match="period"):
        service.add_bill(conn, 10, "2026-10-01", category="tax", period="2026-10")


def test_obligation_bill_must_match_its_asset_and_category(conn):
    luz = ob(conn)
    service.add_asset(conn, "Car", "car")
    with pytest.raises(ValidationError, match='belongs to asset "h"'):
        service.add_bill(conn, 10, "2026-10-01", obligation_id=luz.id, asset_id="car")
    with pytest.raises(ValidationError, match='category "utilities"'):
        service.add_bill(conn, 10, "2026-10-01", obligation_id=luz.id, category="tax")
    b = service.add_bill(
        conn, 10, "2026-10-01", obligation_id=luz.id, asset_id="h", category="Utilities"
    )
    assert b.period == "2026-10"


def test_real_bill_replaces_the_generated_one(conn):
    luz = ob(conn, due_day=10)
    generated = repo.find_period_bill(conn, luz.id, "2026-10")
    assert generated.estimated

    real = service.add_bill(
        conn, 72_300.5, "2026-10-12", obligation_id=luz.id, provider="Edenor"
    )
    assert real.id == generated.id
    assert (real.amount, real.estimated, real.due_date, real.provider) == (
        72_300.5,
        False,
        "2026-10-12",
        "Edenor",
    )
    assert len(service.list_bills(conn)) == 1


def test_bill_dates_are_validated(conn):
    with pytest.raises(ValidationError, match="due_date"):
        service.add_bill(conn, 10, "2026-13-01", category="tax")
    with pytest.raises(ValidationError, match="due_date"):
        service.add_bill(conn, 10, "20261001", category="tax")
    with pytest.raises(ValidationError, match="amount"):
        service.add_bill(conn, -1, "2026-10-01", category="tax")


def test_paying_stamps_and_unpaying_clears(conn):
    b = service.add_bill(conn, 10, "2026-10-01", category="tax")
    paid = service.update_bill(conn, b.id, status="paid")
    assert paid.paid_at is not None
    # Paying again keeps the first timestamp.
    assert service.update_bill(conn, b.id, status="paid").paid_at == paid.paid_at
    assert service.update_bill(conn, b.id, status="pending").paid_at is None


def test_changed_amount_is_no_longer_an_estimate(conn):
    luz = ob(conn, due_day=10)
    b = repo.find_period_bill(conn, luz.id, "2026-10")
    assert service.update_bill(conn, b.id, amount=b.amount).estimated
    assert not service.update_bill(conn, b.id, amount=999).estimated


def test_moving_the_due_date_keeps_the_period(conn):
    luz = ob(conn)
    b = service.add_bill(conn, 10, "2026-10-31", obligation_id=luz.id)
    moved = service.update_bill(conn, b.id, due_date="2026-11-01")
    assert moved.period == "2026-10"


def test_period_conflicts_are_rejected(conn):
    luz = ob(conn)
    service.add_bill(conn, 10, "2026-10-10", obligation_id=luz.id)
    nov = service.add_bill(conn, 10, "2026-11-10", obligation_id=luz.id)
    with pytest.raises(Conflict, match="already has bill"):
        service.update_bill(conn, nov.id, period="2026-10")


def test_detaching_a_bill_keeps_its_asset_and_category(conn):
    luz = ob(conn)
    b = service.add_bill(conn, 10, "2026-10-10", obligation_id=luz.id)
    one_off = service.update_bill(conn, b.id, obligation_id=None)
    assert (one_off.obligation_id, one_off.asset_id, one_off.category, one_off.period) == (
        None,
        "h",
        "utilities",
        None,
    )


def test_attaching_a_one_off_takes_the_due_month(conn):
    luz = ob(conn)
    b = service.add_bill(conn, 10, "2026-10-10", category="other")
    attached = service.update_bill(conn, b.id, obligation_id=luz.id)
    assert (attached.period, attached.category, attached.asset_id) == (
        "2026-10",
        "utilities",
        "h",
    )


def test_obligation_bills_follow_obligation_changes(conn):
    luz = ob(conn)
    b = service.add_bill(conn, 10, "2026-10-10", obligation_id=luz.id)
    service.update_obligation(conn, luz.id, category="Energy")
    assert service.get_bill(conn, b.id).category == "energy"


def test_list_bills_filters(conn):
    luz = ob(conn)
    service.add_bill(conn, 10, "2026-10-10", obligation_id=luz.id)
    service.add_bill(conn, 20, "2026-10-05", category="tax")
    assert [b.amount for b in service.list_bills(conn)] == [20, 10]
    assert [b.amount for b in service.list_bills(conn, asset_id="h")] == [10]
    assert [b.amount for b in service.list_bills(conn, category="tax")] == [20]
    with pytest.raises(ValidationError):
        service.list_bills(conn, status="late")


def test_missing_records_raise_not_found(conn):
    with pytest.raises(NotFound):
        service.get_bill(conn, 99)
    with pytest.raises(NotFound):
        service.remove_bill(conn, 99)
    with pytest.raises(NotFound):
        service.update_asset(conn, "nope", name="x")


def test_the_database_path_is_required(monkeypatch):
    # No default inside the code folder: lo9i replaces that folder on update.
    monkeypatch.delenv("EXPENSES_DB", raising=False)
    with pytest.raises(db.MissingDatabaseError):
        db.connect()
