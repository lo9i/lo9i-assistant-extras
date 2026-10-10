from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from lo9i_calendar import times
from lo9i_calendar.model import AllDay, CalendarError, Repeat, Timed

from .conftest import ZONE

TZ = ZoneInfo(ZONE)


def test_the_users_zone_is_tz_or_utc(monkeypatch):
    assert times.user_zone().key == ZONE
    monkeypatch.setenv("TZ", "Not/AZone")
    assert times.user_zone().key == "UTC"


def test_a_date_or_a_time_in_the_users_zone_unless_it_has_an_offset():
    assert times.parse("2026-10-12", TZ) == date(2026, 10, 12)
    assert times.parse("2026-10-12T09:00", TZ) == datetime(2026, 10, 12, 9, tzinfo=TZ)
    assert times.show(times.parse("2026-10-12T12:00Z", TZ), TZ) == "2026-10-12T09:00-03:00"
    with pytest.raises(CalendarError, match="isn't a date"):
        times.parse("tomorrow", TZ)


def test_a_range_of_dates_includes_the_last_day():
    begin, finish = times.span("2026-10-12", "2026-10-13", TZ)
    assert (begin, finish) == (datetime(2026, 10, 12, tzinfo=TZ), datetime(2026, 10, 14, tzinfo=TZ))
    assert times.span("2026-10-12T15:00", "", TZ)[1] == datetime(2026, 10, 13, tzinfo=TZ)
    assert times.span("", "", TZ)[0].date() == datetime.now(TZ).date()
    with pytest.raises(CalendarError, match="before the start"):
        times.span("2026-10-12", "2026-10-11", TZ)
    with pytest.raises(CalendarError, match="at most 366 days"):
        times.span("2026-01-01", "2027-06-01", TZ)


def test_a_new_events_timing():
    nine, ten = datetime(2026, 10, 12, 9, tzinfo=TZ), datetime(2026, 10, 12, 10, tzinfo=TZ)
    assert times.timing("2026-10-12T09:00", "", False, TZ) == Timed(nine, ten, TZ)
    assert times.timing("2026-10-12T09:00", "", True, TZ) == AllDay(date(2026, 10, 12), date(2026, 10, 12))
    assert times.timing("2026-10-12", "2026-10-14", False, TZ) == AllDay(date(2026, 10, 12), date(2026, 10, 14))
    with pytest.raises(CalendarError, match="needs a time too"):
        times.timing("2026-10-12T09:00", "2026-10-13", False, TZ)
    with pytest.raises(CalendarError, match="after the start"):
        times.timing("2026-10-12T09:00", "2026-10-12T08:00", False, TZ)


def test_repeat_takes_the_rrule_parts_every_source_saves():
    assert times.repeat("") is None
    assert times.repeat("RRULE:freq=weekly;interval=2;byday=MO,WE;until=20261231") == Repeat(
        "WEEKLY", 2, 0, date(2026, 12, 31), ("MO", "WE")
    )
    assert times.repeat("FREQ=MONTHLY;COUNT=6") == Repeat("MONTHLY", count=6)
    for wrong in ("FREQ=HOURLY", "FREQ=MONTHLY;BYMONTHDAY=1", "FREQ=DAILY;BYDAY=MO", "FREQ=WEEKLY;BYDAY=1MO"):
        with pytest.raises(CalendarError):
            times.repeat(wrong)
    with pytest.raises(CalendarError, match="not both"):
        times.repeat("FREQ=DAILY;COUNT=3;UNTIL=20261231")
