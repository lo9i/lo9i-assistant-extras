"""The CalDAV source against a real CalDAV server (Radicale, see conftest.py), through the service."""

import sys
import types
from datetime import datetime

import pytest

from lo9i_calendar import service, times
from lo9i_calendar.caldav_source import CalDav
from lo9i_calendar.model import CalendarError

BREAKFAST = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Other//EN
BEGIN:VEVENT
UID:floating
DTSTAMP:20261001T000000Z
DTSTART:20261013T080000
DURATION:PT30M
SUMMARY:Floating breakfast
TRANSP:TRANSPARENT
END:VEVENT
END:VCALENDAR
"""
REVIEW = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Other//EN
BEGIN:VEVENT
UID:utc
DTSTAMP:20261001T000000Z
DTSTART:20261013T150000Z
DTEND:20261013T160000Z
SUMMARY:Review
STATUS:CONFIRMED
ATTENDEE;CN=Juan Pérez:mailto:juan@example.com
ATTENDEE:mailto:lu@example.com
URL:https://meet.example.com/abc
END:VEVENT
END:VCALENDAR
"""


def _titles(listing):
    return [(e.title, e.start) for e in listing.events]


def test_the_accounts_calendars_are_listed_with_their_ids(dav):
    found, problems = service.calendars()
    assert sorted(c.name for c in found) == ["Home", "Work"] and problems == []
    assert all(c.id.startswith("caldav:/ana/") and c.account == "CalDAV (127.0.0.1)" for c in found)


def test_an_event_is_created_and_listed_in_the_users_time_zone(dav):
    made = service.create("Dentist", "2026-10-12T15:00", calendar="work", location="Av. Corrientes 1234")
    assert (made.start, made.end, made.calendar) == ("2026-10-12T15:00-03:00", "2026-10-12T16:00-03:00", "Work")
    listing = service.events("2026-10-12")
    assert [(e.id, e.title, e.location) for e in listing.events] == [(made.id, "Dentist", "Av. Corrientes 1234")]
    assert service.events("2026-10-13").events == []


def test_an_event_in_another_zone_keeps_it(dav):
    made = service.create("Flight to Madrid", "2026-10-20T10:00", "2026-10-20T12:00", timezone="Europe/Madrid")
    assert (made.start, made.timezone) == ("2026-10-20T05:00-03:00", "Europe/Madrid")
    assert "TZID=Europe/Madrid" in dav.work.event_by_url(made.id.removeprefix("caldav:")).data


def test_an_all_day_event_ends_on_its_last_day(dav):
    made = service.create("Trip", "2026-10-14", "2026-10-16", calendar="Home")
    assert (made.all_day, made.start, made.end) == (True, "2026-10-14", "2026-10-16")
    assert _titles(service.events("2026-10-16")) == [("Trip", "2026-10-14")]
    assert service.events("2026-10-17").events == []
    assert "DTEND;VALUE=DATE:20261017" in dav.home.event_by_url(made.id.removeprefix("caldav:")).data


def test_events_from_other_clients_read_right(dav):
    dav.work.add_event(BREAKFAST)
    dav.work.add_event(REVIEW)
    floating, review = service.events("2026-10-13").events
    assert (floating.title, floating.start, floating.end, floating.busy) == (
        "Floating breakfast",
        "2026-10-13T08:00-03:00",
        "2026-10-13T08:30-03:00",
        False,
    )
    assert (review.start, review.attendees, review.url) == (
        "2026-10-13T12:00-03:00",
        ["Juan Pérez", "lu@example.com"],
        "https://meet.example.com/abc",
    )


def test_a_repeating_event_comes_as_each_occurrence(dav):
    made = service.create("Standup", "2026-10-12T09:00", repeat="FREQ=WEEKLY;COUNT=3")
    assert made.recurring and made.occurrence == ""
    listing = service.events("2026-10-01", "2026-11-30")
    assert [e.occurrence for e in listing.events] == [
        "2026-10-12T09:00-03:00",
        "2026-10-19T09:00-03:00",
        "2026-10-26T09:00-03:00",
    ]
    assert service.event(made.id, "2026-10-19T09:00-03:00").start == "2026-10-19T09:00-03:00"
    with pytest.raises(CalendarError, match="no occurrence at"):
        service.event(made.id, "2026-10-20T09:00")


def test_until_includes_its_day(dav):
    service.create("Gym", "2026-10-12T19:00", repeat="FREQ=DAILY;UNTIL=20261014")
    assert [e.start[:10] for e in service.events("2026-10-12", "2026-10-20").events] == [
        "2026-10-12",
        "2026-10-13",
        "2026-10-14",
    ]


def test_one_occurrence_changes_or_goes_and_the_rest_stay(dav):
    made = service.create("Standup", "2026-10-12T09:00", repeat="FREQ=WEEKLY;COUNT=3")
    moved = service.update(made.id, "2026-10-19T09:00", start="2026-10-19T11:00", title="Standup (late)")
    assert (moved.start, moved.end, moved.occurrence) == (
        "2026-10-19T11:00-03:00",
        "2026-10-19T12:00-03:00",
        "2026-10-19T09:00-03:00",
    )
    service.delete(made.id, "2026-10-26T09:00-03:00")
    assert _titles(service.events("2026-10-01", "2026-11-30")) == [
        ("Standup", "2026-10-12T09:00-03:00"),
        ("Standup (late)", "2026-10-19T11:00-03:00"),
    ]


def test_moving_the_series_keeps_its_changed_and_deleted_occurrences(dav):
    made = service.create("Standup", "2026-10-12T09:00", repeat="FREQ=WEEKLY;COUNT=3")
    service.update(made.id, "2026-10-19T09:00", title="Standup (late)", start="2026-10-19T11:00")
    service.delete(made.id, "2026-10-26T09:00")
    series = service.update(made.id, start="2026-10-12T10:00")
    assert (series.start, series.end, series.occurrence) == ("2026-10-12T10:00-03:00", "2026-10-12T11:00-03:00", "")
    assert _titles(service.events("2026-10-01", "2026-11-30")) == [
        ("Standup", "2026-10-12T10:00-03:00"),
        ("Standup (late)", "2026-10-19T11:00-03:00"),
    ]


def test_an_update_changes_only_whats_given(dav):
    made = service.create("Lunch", "2026-10-12T13:00", "2026-10-12T14:30", location="Café", notes="Bring the keys")
    changed = service.update(made.id, start="2026-10-12T12:00", location="")
    assert (changed.start, changed.end, changed.location, changed.notes) == (
        "2026-10-12T12:00-03:00",
        "2026-10-12T13:30-03:00",
        "",
        "Bring the keys",
    )
    whole_day = service.update(made.id, all_day=True)
    assert (whole_day.all_day, whole_day.start, whole_day.end) == (True, "2026-10-12", "2026-10-12")


def test_search_and_calendar_filters(dav):
    service.create("Dentist", "2026-10-12T15:00", calendar="Work", notes="Dr. Gómez")
    service.create("Football", "2026-10-12T19:00", calendar="Home")
    assert _titles(service.events("2026-10-12", query="gómez")) == [("Dentist", "2026-10-12T15:00-03:00")]
    assert _titles(service.events("2026-10-12", calendar="Home")) == [("Football", "2026-10-12T19:00-03:00")]
    with pytest.raises(CalendarError, match=r"There's no calendar 'Gym'\. The calendars are: Home, Work\."):
        service.events("2026-10-12", calendar="Gym")


def test_a_deleted_event_is_gone(dav):
    made = service.create("Dentist", "2026-10-12T15:00")
    service.delete(made.id)
    assert service.events("2026-10-12").events == []
    with pytest.raises(CalendarError, match="no event with that id"):
        service.event(made.id)


def test_a_wrong_password_says_so(dav, monkeypatch):
    monkeypatch.setenv("CALDAV_PASSWORD", "wrong")
    with pytest.raises(CalendarError, match="refused the username or password"):
        service.calendars()


def test_an_unreachable_server_says_so(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("CALDAV_URL", "http://127.0.0.1:9/")
    with pytest.raises(CalendarError, match=r"CalDAV \(127.0.0.1\) couldn't be reached"):
        service.events("2026-10-12")


def test_an_event_that_cant_be_read_is_left_out():
    broken = types.SimpleNamespace(data="BEGIN:VCALENDAR\nBEGIN:VEVENT\nRRULE:FREQ=SOMETIMES\n", url="/ana/work/x.ics")
    tz = times.user_zone()
    start, end = datetime(2026, 10, 12, tzinfo=tz), datetime(2026, 10, 13, tzinfo=tz)
    assert CalDav("https://dav.example.com", "ana", "secret")._occurrences(None, broken, start, end, tz) == []
