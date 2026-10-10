"""The service over fake sources: routing, several sources, calendar names, free time."""

from dataclasses import replace
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from lo9i_calendar import service
from lo9i_calendar.model import AllDay, Calendar, CalendarError, Changes, Event, Timed

from .conftest import ZONE


class FakeSource:
    def __init__(self, key, calendars, events=(), fail=None):
        self.key, self.name = key, key.title()
        self._calendars, self._events, self.fail = calendars, list(events), fail
        self.created, self.updated, self.deleted = [], [], []

    def calendars(self):
        if self.fail:
            raise CalendarError(self.fail)
        return self._calendars

    def events(self, start, end, calendar_ids):
        if self.fail:
            raise CalendarError(self.fail)
        return [e for e in self._events if calendar_ids is None or e.calendar_id.split(":", 1)[1] in calendar_ids]

    def event(self, event_id, occurrence):
        return next(e for e in self._events if e.id == f"{self.key}:{event_id}")

    def create(self, calendar_id, new):
        self.created.append((calendar_id, new))
        return _event(f"{self.key}:new", new.title, "2026-10-12T09:00-03:00", "2026-10-12T10:00-03:00")

    def update(self, event_id, occurrence, changes: Changes):
        self.updated.append((event_id, occurrence, changes))
        return self.event(event_id, occurrence)

    def delete(self, event_id, occurrence):
        self.deleted.append((event_id, occurrence))


def _event(id, title, start, end, calendar_id="", **more):
    all_day = len(start) == 10
    return Event(id, calendar_id or id.split(":")[0] + ":cal", "Cal", title, start, end, all_day, **more)


@pytest.fixture
def sources(monkeypatch):
    def use(*found):
        monkeypatch.setattr(service, "sources", lambda: list(found))

    return use


MAC_WORK = Calendar("mac:work", "Work", "iCloud", default=True)
MAC_BDAYS = Calendar("mac:bdays", "Birthdays", "Other", writable=False)
DAV_WORK = Calendar("caldav:/w/", "Work", "CalDAV (dav.example.com)")


def test_ids_route_to_their_source(sources):
    mac, dav = (
        FakeSource("mac", [MAC_WORK]),
        FakeSource(
            "caldav", [DAV_WORK], [_event("caldav:/w/1.ics", "A", "2026-10-12T09:00-03:00", "2026-10-12T10:00-03:00")]
        ),
    )
    sources(mac, dav)
    service.delete("caldav:/w/1.ics", "2026-10-12T09:00")
    assert dav.deleted[0][0] == "/w/1.ics" and dav.deleted[0][1].hour == 9 and mac.deleted == []
    with pytest.raises(CalendarError, match="isn't an id from these calendars"):
        service.event("google:123")


def test_a_failing_source_doesnt_hide_the_others(sources):
    good = FakeSource("mac", [MAC_WORK], [_event("mac:1", "A", "2026-10-12T09:00-03:00", "2026-10-12T10:00-03:00")])
    sources(good, FakeSource("caldav", [], fail="Wrong password."))
    listing = service.events("2026-10-12")
    assert [e.title for e in listing.events] == ["A"] and listing.problems == ["Caldav: Wrong password."]
    sources(FakeSource("caldav", [], fail="Wrong password."))
    with pytest.raises(CalendarError, match="Wrong password"):
        service.events("2026-10-12")


def test_too_many_events_are_cut_and_said(sources):
    many = [_event(f"mac:{n}", f"E{n:03d}", "2026-10-12T09:00-03:00", "2026-10-12T10:00-03:00") for n in range(205)]
    sources(FakeSource("mac", [MAC_WORK], many))
    listing = service.events("2026-10-12")
    assert len(listing.events) == service.MAX_EVENTS and listing.more


def test_a_new_event_goes_to_the_named_calendar_or_the_default(sources):
    mac, dav = FakeSource("mac", [MAC_WORK, MAC_BDAYS]), FakeSource("caldav", [DAV_WORK])
    sources(mac, dav)
    service.create("Dentist", "2026-10-12T15:00")
    assert mac.created[0][0] == "work"
    with pytest.raises(CalendarError, match=r"Several calendars are called that.*mac:work.*caldav:/w/"):
        service.create("Dentist", "2026-10-12T15:00", calendar="work")
    service.create("Dentist", "2026-10-12T15:00", calendar="caldav:/w/")
    assert dav.created[0][0] == "/w/"
    with pytest.raises(CalendarError, match="can't be changed"):
        service.create("Party", "2026-10-12T20:00", calendar="Birthdays")
    with pytest.raises(CalendarError, match="needs a title"):
        service.create(" ", "2026-10-12T20:00")


def test_a_new_start_keeps_the_events_length(sources):
    lunch = _event("mac:1", "Lunch", "2026-10-12T13:00-03:00", "2026-10-12T14:30-03:00")
    trip = _event("mac:2", "Trip", "2026-10-14", "2026-10-16")
    mac = FakeSource("mac", [MAC_WORK], [lunch, trip])
    sources(mac)
    service.update("mac:1", start="2026-10-13T12:00")
    tz = ZoneInfo(ZONE)
    assert mac.updated[-1][2].timing == Timed(
        datetime(2026, 10, 13, 12, tzinfo=tz), datetime(2026, 10, 13, 13, 30, tzinfo=tz), tz
    )
    service.update("mac:2", start="2026-10-20")
    assert mac.updated[-1][2].timing == AllDay(date(2026, 10, 20), date(2026, 10, 22))
    service.update("mac:1", all_day=True)
    assert mac.updated[-1][2].timing == AllDay(date(2026, 10, 12), date(2026, 10, 12))
    with pytest.raises(CalendarError, match="Give the start as a time"):
        service.update("mac:2", all_day=False)
    service.update("mac:1", title="Long lunch")
    assert mac.updated[-1][2] == Changes(title="Long lunch")


def test_free_busy_merges_events_and_finds_the_gaps(sources):
    events = [
        _event("mac:1", "Standup", "2026-10-12T09:00-03:00", "2026-10-12T09:30-03:00"),
        _event("mac:2", "Review", "2026-10-12T09:15-03:00", "2026-10-12T10:00-03:00"),
        _event("mac:3", "Lunch", "2026-10-12T13:00-03:00", "2026-10-12T14:00-03:00"),
        _event("mac:4", "Focus", "2026-10-12T15:00-03:00", "2026-10-12T16:00-03:00", busy=False),
        _event("mac:5", "Coffee", "2026-10-12T17:45-03:00", "2026-10-12T18:30-03:00"),
        _event("mac:6", "Holiday", "2026-10-13", "2026-10-13"),
    ]
    sources(FakeSource("mac", [MAC_WORK], events))
    result = service.free_busy("2026-10-12", "2026-10-13")
    assert result["busy"] == [
        {"start": "2026-10-12T09:00-03:00", "end": "2026-10-12T10:00-03:00", "events": ["Standup", "Review"]},
        {"start": "2026-10-12T13:00-03:00", "end": "2026-10-12T14:00-03:00", "events": ["Lunch"]},
        {"start": "2026-10-12T17:45-03:00", "end": "2026-10-12T18:30-03:00", "events": ["Coffee"]},
    ]
    assert result["free"] == [
        {"start": "2026-10-12T10:00-03:00", "end": "2026-10-12T13:00-03:00"},
        {"start": "2026-10-12T14:00-03:00", "end": "2026-10-12T17:45-03:00"},
        {"start": "2026-10-13T09:00-03:00", "end": "2026-10-13T18:00-03:00"},
    ]
    assert result["all_day"] == [{"start": "2026-10-13", "end": "2026-10-13", "title": "Holiday"}]
    short = service.free_busy("2026-10-12T12:00", "2026-10-12T15:00", hours="08:00-20:00", min_minutes=90)
    assert short["free"] == []
    with pytest.raises(CalendarError, match="range of hours"):
        service.free_busy("2026-10-12", hours="9 to 5")


def test_a_calendar_name_filters_across_sources(sources):
    mac_event = _event("mac:1", "A", "2026-10-12T09:00-03:00", "2026-10-12T10:00-03:00", calendar_id="mac:work")
    dav_event = _event("caldav:1", "B", "2026-10-12T11:00-03:00", "2026-10-12T12:00-03:00", calendar_id="caldav:/w/")
    other = replace(mac_event, id="mac:2", title="C", calendar_id="mac:bdays")
    sources(FakeSource("mac", [MAC_WORK, MAC_BDAYS], [mac_event, other]), FakeSource("caldav", [DAV_WORK], [dav_event]))
    assert [e.title for e in service.events("2026-10-12", calendar="Work").events] == ["A", "B"]
