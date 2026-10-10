"""Assets, the obligations paid for them (or on their own), and the bills.

Every rule lives here so the CLI and the MCP server behave the same. Errors
name the valid choices, so a caller that guessed wrong can fix it without a
second lookup.

Update functions take UNSET for "leave as is". None, where allowed, clears
the field.
"""

import math
import re
import sqlite3
import unicodedata
from datetime import date
from typing import Any

from . import recurring, repo
from .models import Asset, Bill, Obligation

UNSET: Any = object()

ASSET_KINDS = ("house", "car", "other")
EVERY_MONTHS = (1, 2, 3, 6, 12)
STATUSES = ("pending", "paid")


class ExpensesError(Exception):
    pass


class ValidationError(ExpensesError):
    pass


class NotFound(ExpensesError):
    pass


class Conflict(ExpensesError):
    pass


# --- Input checks ---


def slugify(s: str) -> str:
    """Lowercase ASCII slug, e.g. "Casa Peñalolén" -> "casa-penalolen"."""
    ascii_ = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return "-".join(p for p in re.split(r"[^a-z0-9]+", ascii_.lower()) if p)


def _required(s: str, field: str) -> str:
    s = s.strip()
    if not s:
        raise ValidationError(f"{field} is required")
    return s


def _choice(value: Any, choices: tuple, field: str) -> Any:
    if value not in choices:
        raise ValidationError(f"{field} must be one of {list(choices)}, got {value!r}")
    return value


def _amount(a: float) -> float:
    if isinstance(a, bool) or not isinstance(a, (int, float)) or not math.isfinite(a) or a < 0:
        raise ValidationError(f"amount must be a non-negative number, got {a!r}")
    return round(float(a), 2)


def _date(d: str) -> str:
    """Only YYYY-MM-DD: fromisoformat also takes 20261008 and week dates (2026-W41-3)."""
    d = d.strip()
    try:
        valid = date.fromisoformat(d).isoformat() == d
    except ValueError:
        valid = False
    if not valid:
        raise ValidationError(f'due_date must be YYYY-MM-DD, got "{d}"')
    return d


def _period(p: str) -> str:
    p = p.strip()
    try:
        valid = date.fromisoformat(f"{p}-01").isoformat()[:7] == p
    except ValueError:
        valid = False
    if not valid:
        raise ValidationError(f'period must be YYYY-MM, got "{p}"')
    return p


def _int_in(value: int, lo: int, hi: int, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        raise ValidationError(f"{field} must be between {lo} and {hi}, got {value!r}")
    return value


def _metadata(raw: dict[str, str] | None) -> dict[str, str]:
    """Trim keys and values and reject blank keys, so "client_number " and
    "client_number" can't both exist."""
    out: dict[str, str] = {}
    for k, v in (raw or {}).items():
        k = str(k).strip()
        if not k:
            raise ValidationError("metadata keys must not be empty")
        if k in out:
            raise ValidationError(f'duplicate metadata key "{k}"')
        out[k] = str(v).strip()
    return out


def _schedule(every_months: int, anchor_month: int | None) -> int | None:
    """Validated anchor month. Monthly obligations don't need one."""
    _choice(every_months, EVERY_MONTHS, "every_months")
    if every_months == 1:
        return None
    if anchor_month is None:
        raise ValidationError(
            "anchor_month (1-12, a month the bill falls on) is required when every_months > 1"
        )
    return _int_in(anchor_month, 1, 12, "anchor_month")


# --- Lookups that fail with the valid choices ---


def _asset_id(conn: sqlite3.Connection, id: str) -> str:
    id = id.strip()
    if repo.get_asset(conn, id):
        return id
    ids = [a.id for a in repo.list_assets(conn)]
    raise ValidationError(f'unknown asset "{id}", expected one of {ids}')


def _obligation(conn: sqlite3.Connection, id: int) -> Obligation:
    ob = repo.get_obligation(conn, id)
    if ob is None:
        known = [f"{o.id} ({o.name})" for o in repo.list_obligations(conn)]
        raise ValidationError(f"unknown obligation {id}, expected one of {known}")
    return ob


def _category(conn: sqlite3.Connection, name: str) -> str:
    """Normalized category name, created when it doesn't exist yet."""
    name = " ".join(name.lower().split())
    if not name:
        raise ValidationError("category must not be empty")
    repo.insert_category(conn, name)
    return name


def _match_obligation(ob: Obligation, asset_id: str | None, category: str | None) -> None:
    """An obligation's bill takes its asset and category from the obligation.
    Values the caller also gives must agree with it."""
    if asset_id is not None and asset_id.strip() != ob.asset_id:
        raise ValidationError(
            f'obligation {ob.id} ({ob.name}) belongs to asset "{ob.asset_id}", not "{asset_id}"'
        )
    if category is not None and " ".join(category.lower().split()) != ob.category:
        raise ValidationError(
            f'obligation {ob.id} ({ob.name}) has category "{ob.category}", not "{category}"'
        )


# --- Assets ---


def list_assets(conn: sqlite3.Connection) -> list[Asset]:
    return repo.list_assets(conn)


def get_asset(conn: sqlite3.Connection, id: str) -> Asset:
    asset = repo.get_asset(conn, id)
    if asset is None:
        raise NotFound(f'asset "{id}" not found')
    return asset


def add_asset(
    conn: sqlite3.Connection,
    name: str,
    kind: str = "other",
    *,
    id: str | None = None,
    notes: str = "",
    metadata: dict[str, str] | None = None,
) -> Asset:
    """The id is a slug of `id`, or of `name` when absent."""
    name = _required(name, "name")
    _choice(kind, ASSET_KINDS, "kind")
    asset_id = slugify(id or name)
    if not asset_id:
        raise ValidationError("id must contain letters or digits")
    with conn:
        if not repo.insert_asset(conn, asset_id, name, kind, notes.strip(), _metadata(metadata)):
            raise Conflict(f'asset "{asset_id}" already exists')
    return get_asset(conn, asset_id)


def update_asset(
    conn: sqlite3.Connection,
    id: str,
    *,
    name: str = UNSET,
    kind: str = UNSET,
    notes: str = UNSET,
    metadata: dict[str, str] = UNSET,
) -> Asset:
    """`metadata` replaces the stored object whole."""
    get_asset(conn, id)
    fields: dict[str, Any] = {}
    if name is not UNSET:
        fields["name"] = _required(name, "name")
    if kind is not UNSET:
        fields["kind"] = _choice(kind, ASSET_KINDS, "kind")
    if notes is not UNSET:
        fields["notes"] = notes.strip()
    if metadata is not UNSET:
        fields["metadata"] = _metadata(metadata)
    with conn:
        repo.update_asset(conn, id, fields)
    return get_asset(conn, id)


def remove_asset(conn: sqlite3.Connection, id: str) -> None:
    """Refuses while the asset still has bills, so history is never orphaned.
    Its obligations go with it."""
    get_asset(conn, id)
    n = repo.count_asset_bills(conn, id)
    if n:
        raise Conflict(f'asset "{id}" has {n} bill(s), delete them first')
    with conn:
        repo.delete_asset(conn, id)


# --- Categories ---


def list_categories(conn: sqlite3.Connection) -> list[str]:
    return repo.list_categories(conn)


def add_category(conn: sqlite3.Connection, name: str) -> str:
    """Idempotent. Categories are also created on first use."""
    with conn:
        return _category(conn, name)


def remove_category(conn: sqlite3.Connection, name: str) -> None:
    name = " ".join(name.lower().split())
    n = repo.count_category_uses(conn, name)
    if n:
        raise Conflict(f'category "{name}" is used by {n} obligation(s) or bill(s)')
    with conn:
        if not repo.delete_category(conn, name):
            raise NotFound(f'category "{name}" not found')


# --- Obligations ---


def list_obligations(
    conn: sqlite3.Connection, asset_id: str | None = None, category: str | None = None
) -> list[Obligation]:
    return repo.list_obligations(conn, asset_id, category)


def get_obligation(conn: sqlite3.Connection, id: int) -> Obligation:
    ob = repo.get_obligation(conn, id)
    if ob is None:
        raise NotFound(f"obligation {id} not found")
    return ob


def _name_taken(conn: sqlite3.Connection, asset_id: str | None, name: str, skip: int | None) -> bool:
    return any(
        o.id != skip and o.asset_id == asset_id and o.name.lower() == name.lower()
        for o in repo.list_obligations(conn, asset_id)
    )


def add_obligation(
    conn: sqlite3.Connection,
    name: str,
    category: str,
    *,
    asset_id: str | None = None,
    notes: str = "",
    metadata: dict[str, str] | None = None,
    due_day: int | None = None,
    every_months: int = 1,
    anchor_month: int | None = None,
    expected_amount: float | None = None,
    today: date | None = None,
) -> Obligation:
    """With a `due_day`, any bill the obligation makes due is created right
    away."""
    name = _required(name, "name")
    if due_day is not None:
        _int_in(due_day, 1, 31, "due_day")
    anchor_month = _schedule(every_months, anchor_month)
    if expected_amount is not None:
        expected_amount = _amount(expected_amount)
    with conn:
        if asset_id is not None:
            asset_id = _asset_id(conn, asset_id)
        if _name_taken(conn, asset_id, name, None):
            raise Conflict(f'"{asset_id or "no asset"}" already has obligation "{name}"')
        id = repo.insert_obligation(
            conn,
            asset_id=asset_id,
            name=name,
            category=_category(conn, category),
            notes=notes.strip(),
            metadata=_metadata(metadata),
            due_day=due_day,
            every_months=every_months,
            anchor_month=anchor_month,
            expected_amount=expected_amount,
        )
        if due_day is not None:
            recurring._generate(conn, today or date.today())
    return get_obligation(conn, id)


def update_obligation(
    conn: sqlite3.Connection,
    id: int,
    *,
    name: str = UNSET,
    category: str = UNSET,
    notes: str = UNSET,
    metadata: dict[str, str] = UNSET,
    due_day: int | None = UNSET,
    every_months: int = UNSET,
    anchor_month: int | None = UNSET,
    expected_amount: float | None = UNSET,
    today: date | None = None,
) -> Obligation:
    """`due_day=None` stops it repeating. `expected_amount=None` goes back to
    estimating from the last bill. `metadata` replaces the stored object.
    Pending bills made ahead of time follow a new due day or fixed amount."""
    cur = get_obligation(conn, id)
    fields: dict[str, Any] = {}
    if name is not UNSET:
        fields["name"] = _required(name, "name")
        if _name_taken(conn, cur.asset_id, fields["name"], id):
            raise Conflict(f'"{cur.asset_id or "no asset"}" already has obligation "{name}"')
    if notes is not UNSET:
        fields["notes"] = notes.strip()
    if metadata is not UNSET:
        fields["metadata"] = _metadata(metadata)
    if due_day is not UNSET:
        fields["due_day"] = None if due_day is None else _int_in(due_day, 1, 31, "due_day")
    if expected_amount is not UNSET:
        fields["expected_amount"] = None if expected_amount is None else _amount(expected_amount)
    if every_months is not UNSET or anchor_month is not UNSET:
        every = cur.every_months if every_months is UNSET else every_months
        anchor = cur.anchor_month if anchor_month is UNSET else anchor_month
        fields["every_months"] = every
        fields["anchor_month"] = _schedule(every, anchor)
    with conn:
        if category is not UNSET:
            fields["category"] = _category(conn, category)
        repo.update_obligation(conn, id, fields)
        _reschedule_bills(conn, cur, fields, today or date.today())
        if fields.get("due_day", cur.due_day) is not None:
            recurring._generate(conn, today or date.today())
    return get_obligation(conn, id)


def _reschedule_bills(conn: sqlite3.Connection, before: Obligation, fields: dict[str, Any], today: date) -> None:
    """Pending bills from this month on take a new due day or fixed amount,
    where they still have the schedule's: the old due date, an estimate or
    the old fixed amount. A date or amount the user gave stays."""
    new_day = fields.get("due_day")
    day_changed = new_day is not None and before.due_day is not None and new_day != before.due_day
    amount_changed = "expected_amount" in fields and fields["expected_amount"] != before.expected_amount
    if not (day_changed or amount_changed):
        return
    for b in repo.list_bills(conn, status="pending"):
        if b.obligation_id != before.id or b.period < recurring.period_of(today):
            continue
        changes: dict[str, Any] = {}
        year, month = int(b.period[:4]), int(b.period[5:])
        if day_changed and b.due_date == recurring.due_date_in(year, month, before.due_day).isoformat():
            changes["due_date"] = recurring.due_date_in(year, month, new_day).isoformat()
        if amount_changed and (b.estimated or b.amount == before.expected_amount):
            expected = fields["expected_amount"]
            changes |= {"estimated": True} if expected is None else {"amount": expected, "estimated": False}
        repo.update_bill(conn, b.id, changes)


def remove_obligation(conn: sqlite3.Connection, id: int) -> None:
    """Refuses while bills point at the obligation."""
    get_obligation(conn, id)
    n = repo.count_obligation_bills(conn, id)
    if n:
        raise Conflict(f"obligation {id} has {n} bill(s), delete or reassign them first")
    with conn:
        repo.delete_obligation(conn, id)


# --- Bills ---


def list_bills(
    conn: sqlite3.Connection,
    asset_id: str | None = None,
    category: str | None = None,
    status: str | None = None,
) -> list[Bill]:
    if status is not None:
        _choice(status, STATUSES, "status")
    return repo.list_bills(conn, asset_id, category, status)


def month_bills(conn: sqlite3.Connection, today: date | None = None) -> list[Bill]:
    """This month's bills, plus unpaid ones from earlier months."""
    this = (today or date.today()).strftime("%Y-%m")
    out = []
    for b in repo.list_bills(conn):
        month = b.period or b.due_date[:7]
        if month == this or (month < this and b.status == "pending"):
            out.append(b)
    return out


def totals(bills: list[Bill]) -> dict[str, Any]:
    """Total, paid and pending amounts, plus totals by category and by asset
    ("(none)" for bills without one)."""
    by_category: dict[str, float] = {}
    by_asset: dict[str, float] = {}
    for b in bills:
        by_category[b.category] = by_category.get(b.category, 0) + b.amount
        asset = b.asset_id or "(none)"
        by_asset[asset] = by_asset.get(asset, 0) + b.amount
    total = sum(b.amount for b in bills)
    paid = sum(b.amount for b in bills if b.status == "paid")
    return {
        "total": round(total, 2),
        "paid": round(paid, 2),
        "pending": round(total - paid, 2),
        "by_category": {k: round(v, 2) for k, v in sorted(by_category.items())},
        "by_asset": {k: round(v, 2) for k, v in sorted(by_asset.items())},
    }


def get_bill(conn: sqlite3.Connection, id: int) -> Bill:
    bill = repo.get_bill(conn, id)
    if bill is None:
        raise NotFound(f"bill {id} not found")
    return bill


def add_bill(
    conn: sqlite3.Connection,
    amount: float,
    due_date: str,
    *,
    obligation_id: int | None = None,
    asset_id: str | None = None,
    category: str | None = None,
    period: str | None = None,
    provider: str = "",
    notes: str = "",
) -> Bill:
    """Create a bill.

    With an obligation, the bill takes the obligation's asset and category,
    and `period` (YYYY-MM) defaults to the due date's month. If the
    obligation already has a bill for the period (usually the one created
    ahead of time), that bill is updated instead: this is the real bill
    arriving, so the amount stops being an estimate.

    Without one, `category` is required and `asset_id` is optional."""
    amount = _amount(amount)
    due_date = _date(due_date)
    provider, notes = provider.strip(), notes.strip()

    with conn:
        if obligation_id is None:
            if period is not None:
                raise ValidationError("period only applies to bills with an obligation")
            if category is None:
                raise ValidationError("category is required for a bill without an obligation")
            id = repo.insert_bill(
                conn,
                amount=amount,
                due_date=due_date,
                asset_id=None if asset_id is None else _asset_id(conn, asset_id),
                category=_category(conn, category),
                provider=provider,
                notes=notes,
            )
            return get_bill(conn, id)

        ob = _obligation(conn, obligation_id)
        _match_obligation(ob, asset_id, category)
        period = _period(period) if period is not None else due_date[:7]
        existing = repo.find_period_bill(conn, ob.id, period)
        if existing is None:
            id = repo.insert_bill(
                conn,
                amount=amount,
                due_date=due_date,
                obligation_id=ob.id,
                period=period,
                provider=provider,
                notes=notes,
            )
            return get_bill(conn, id)

        fields: dict[str, Any] = {"amount": amount, "due_date": due_date, "estimated": False}
        # Blank text fields keep what the bill already has.
        if provider:
            fields["provider"] = provider
        if notes:
            fields["notes"] = notes
        repo.update_bill(conn, existing.id, fields)
        return get_bill(conn, existing.id)


def update_bill(
    conn: sqlite3.Connection,
    id: int,
    *,
    obligation_id: int | None = UNSET,
    asset_id: str | None = UNSET,
    category: str = UNSET,
    period: str = UNSET,
    provider: str = UNSET,
    amount: float = UNSET,
    due_date: str = UNSET,
    status: str = UNSET,
    notes: str = UNSET,
) -> Bill:
    """Partial update. `obligation_id=None` turns the bill into a one-off,
    keeping the asset and category it had through the obligation.

    A changed amount is a real one, so it clears `estimated`. Moving to
    "paid" stamps `paid_at`, moving back to "pending" clears it. Moving the
    due date keeps the period, so a bill due a day into the next month stays
    in its own month."""
    cur = get_bill(conn, id)
    fields: dict[str, Any] = {}
    if due_date is not UNSET:
        fields["due_date"] = _date(due_date)
    if amount is not UNSET:
        fields["amount"] = _amount(amount)
        if fields["amount"] != cur.amount:
            fields["estimated"] = False
    if provider is not UNSET:
        fields["provider"] = provider.strip()
    if notes is not UNSET:
        fields["notes"] = notes.strip()
    if status is not UNSET:
        fields["status"] = _choice(status, STATUSES, "status")
        if status == "paid" and cur.status != "paid":
            fields["paid_at"] = repo.now()
        elif status == "pending":
            fields["paid_at"] = None

    final_obligation = cur.obligation_id if obligation_id is UNSET else obligation_id
    with conn:
        if final_obligation is not None:
            ob = _obligation(conn, final_obligation)
            _match_obligation(
                ob,
                None if asset_id is UNSET else asset_id,
                None if category is UNSET else category,
            )
            if period is not UNSET and period is not None:
                final_period = _period(period)
            else:
                final_period = cur.period or fields.get("due_date", cur.due_date)[:7]
            other = repo.find_period_bill(conn, ob.id, final_period)
            if other and other.id != id:
                raise Conflict(
                    f"obligation {ob.id} ({ob.name}) already has bill {other.id} for {final_period}"
                )
            fields |= {
                "obligation_id": ob.id,
                "asset_id": None,
                "category": None,
                "period": final_period,
            }
        else:
            if period is not UNSET and period is not None:
                raise ValidationError("period only applies to bills with an obligation")
            final_asset = cur.asset_id if asset_id is UNSET else asset_id
            final_category = cur.category if category is UNSET else category
            if final_category is None:
                raise ValidationError("category is required for a bill without an obligation")
            fields |= {
                "obligation_id": None,
                "asset_id": None if final_asset is None else _asset_id(conn, final_asset),
                "category": _category(conn, final_category),
                "period": None,
            }
        repo.update_bill(conn, id, fields)
    return get_bill(conn, id)


def remove_bill(conn: sqlite3.Connection, id: int) -> None:
    with conn:
        if not repo.delete_bill(conn, id):
            raise NotFound(f"bill {id} not found")
