import json
import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class Asset:
    """Something owned that generates expenses: a house, a car."""

    id: str
    name: str
    kind: str
    notes: str
    # Free-form facts, e.g. {"address": "...", "plate": "..."}.
    metadata: dict[str, str]
    created_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Asset":
        return cls(**{**dict(row), "metadata": json.loads(row["metadata"])})


@dataclass(frozen=True)
class Obligation:
    """Something paid repeatedly: Luz, ABL, car insurance, monotributo."""

    id: int
    asset_id: str | None
    name: str
    category: str
    notes: str
    # Free-form facts, e.g. {"client_number": "0012345"}.
    metadata: dict[str, str]
    # Day of the month the bill is due. Set = a bill is created ahead of time
    # for each month on the schedule.
    due_day: int | None
    # Months between bills: 1, 2, 3, 6 or 12.
    every_months: int
    # A month (1-12) the schedule falls on. Required when every_months > 1.
    anchor_month: int | None
    # Fixed amount (e.g. rent). Unset = estimate from the last bill.
    expected_amount: float | None
    created_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Obligation":
        return cls(**{**dict(row), "metadata": json.loads(row["metadata"])})


@dataclass(frozen=True)
class Bill:
    id: int
    obligation_id: int | None
    # Effective values: an obligation's bills take them from the obligation.
    asset_id: str | None
    category: str
    # Billing month, YYYY-MM. Obligation bills only, at most one per period.
    period: str | None
    # True while the amount is a guess copied from an earlier bill.
    estimated: bool
    provider: str
    amount: float
    # YYYY-MM-DD.
    due_date: str
    # "pending" or "paid".
    status: str
    paid_at: str | None
    notes: str
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Bill":
        return cls(**{**dict(row), "estimated": bool(row["estimated"])})
