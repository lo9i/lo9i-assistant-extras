"""The Mac's calendars with a fake EventKit, so the tests never touch the user's real calendars. The fake store
keeps series and expands them the way EventKit does: each occurrence a copy, with its occurrenceDate."""

import sys
import types
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from lo9i_calendar import service
from lo9i_calendar.model import CalendarError

from .conftest import ZONE

NOT_DETERMINED, DENIED, FULL, WRITE_ONLY = 0, 2, 3, 4
THIS, FUTURE = 0, 1
BUSY, FREE = 0, 1
CANCELED = 3
TZ = ZoneInfo(ZONE)


def _at(text: str) -> "_Date":
    return _Date(datetime.fromisoformat(text).replace(tzinfo=TZ).timestamp())


class _Date:
    def __init__(self, ts):
        self.ts = ts

    def timeIntervalSince1970(self):  # noqa: N802 - the framework's names
        return self.ts


class _Zone:
    def __init__(self, name):
        self._name = name

    def name(self):
        return self._name


class _Calendar:
    def __init__(self, ident, title, account="iCloud", writable=True):
        self.ident, self._title, self.account, self.writable = ident, title, account, writable

    def calendarIdentifier(self):  # noqa: N802
        return self.ident

    def title(self):
        return self._title

    def source(self):
        return types.SimpleNamespace(title=lambda: self.account)

    def allowsContentModifications(self):  # noqa: N802
        return self.writable


class _Participant:
    def __init__(self, name, email):
        self._name, self.email = name, email

    def name(self):
        return self._name

    def URL(self):  # noqa: N802
        return types.SimpleNamespace(resourceSpecifier=lambda: self.email)


class _Event:
    def __init__(self, **values):
        self.ident = None
        self.cal = None
        self._title = ""
        self.start = self.end = None
        self.all_day = False
        self._location = self._notes = None
        self.zone = None
        self.rules = []
        self.detached = False
        self.scheduled = None
        self._availability, self._status = BUSY, 0
        self.people = []
        self.link = None
        self.__dict__.update(values)

    def copy(self, **values):
        return _Event(**{**self.__dict__, "rules": list(self.rules), **values})

    def eventIdentifier(self):  # noqa: N802
        return self.ident

    def calendar(self):
        return self.cal

    def setCalendar_(self, cal):  # noqa: N802
        self.cal = cal

    def title(self):
        return self._title

    def setTitle_(self, title):  # noqa: N802
        self._title = title

    def startDate(self):  # noqa: N802
        return self.start

    def setStartDate_(self, date):  # noqa: N802
        self.start = date

    def endDate(self):  # noqa: N802
        return self.end

    def setEndDate_(self, date):  # noqa: N802
        self.end = date

    def isAllDay(self):  # noqa: N802
        return self.all_day

    def setAllDay_(self, value):  # noqa: N802
        self.all_day = value

    def location(self):
        return self._location

    def setLocation_(self, text):  # noqa: N802
        self._location = text

    def notes(self):
        return self._notes

    def setNotes_(self, text):  # noqa: N802
        self._notes = text

    def timeZone(self):  # noqa: N802
        return self.zone

    def setTimeZone_(self, zone):  # noqa: N802
        self.zone = zone

    def hasRecurrenceRules(self):  # noqa: N802
        return bool(self.rules)

    def addRecurrenceRule_(self, rule):  # noqa: N802
        self.rules.append(rule)

    def isDetached(self):  # noqa: N802
        return self.detached

    def occurrenceDate(self):  # noqa: N802
        return self.scheduled

    def availability(self):
        return self._availability

    def status(self):
        return self._status

    def attendees(self):
        return self.people

    def URL(self):  # noqa: N802
        return self.link


class _Rule:
    def __init__(self, freq, interval, days, end):
        self.freq, self.interval, self.days, self.end = freq, interval, days, end


_NEW_RULE = (
    "initRecurrenceWithFrequency_interval_daysOfTheWeek_daysOfTheMonth_monthsOfTheYear_weeksOfTheYear_"
    "daysOfTheYear_setPositions_end_"
)


def _rule(freq, interval, days, _months_days, _months, _weeks, _year_days, _positions, end):
    return _Rule(freq, interval, days, end)


def _framework(status=FULL, grant=True, calendars=None):
    """A fake EventKit, and the store every EKEventStore().init() is."""
    cals = calendars or [
        _Calendar("work", "Work"),
        _Calendar("home", "Home"),
        _Calendar("hol", "Holidays", "Other", False),
    ]
    store = types.SimpleNamespace(
        series={}, moved={}, skipped=set(), saves=[], removals=[], fail=None, asked=[], count=0, cals=cals
    )

    def occurrences(master):
        if not master.rules:
            return [master]
        rule = master.rules[0]
        step = timedelta(days=(7 if rule.freq == 1 else 1) * rule.interval)
        times = rule.end[1] if rule.end and rule.end[0] == "count" else 10
        found = []
        for n in range(times):
            shift = (step * n).total_seconds()
            key = (master.ident, master.start.ts + shift)
            if key in store.skipped:
                continue
            found.append(
                store.moved.get(key)
                or master.copy(
                    start=_Date(master.start.ts + shift),
                    end=_Date(master.end.ts + shift),
                    scheduled=_Date(master.start.ts + shift),
                )
            )
        return found

    class Store:
        @staticmethod
        def authorizationStatusForEntityType_(_kind):  # noqa: N802
            return status

        @classmethod
        def alloc(cls):
            return cls()

        def init(self):
            return self

        def requestFullAccessToEventsWithCompletion_(self, done):  # noqa: N802
            store.asked.append(True)
            done(grant, None)

        def calendarsForEntityType_(self, _kind):  # noqa: N802
            return store.cals

        def calendarWithIdentifier_(self, ident):  # noqa: N802
            return next((c for c in store.cals if c.ident == ident), None)

        def defaultCalendarForNewEvents(self):  # noqa: N802
            return store.cals[0]

        def predicateForEventsWithStartDate_endDate_calendars_(self, start, end, cals):  # noqa: N802
            return start.ts, end.ts, cals

        def eventsMatchingPredicate_(self, predicate):  # noqa: N802
            start, end, cals = predicate
            found = [o for m in store.series.values() for o in occurrences(m)]
            return [o for o in found if o.start.ts < end and o.end.ts > start and (cals is None or o.cal in cals)]

        def eventWithIdentifier_(self, ident):  # noqa: N802
            master = store.series.get(ident)
            return master and (master.copy(scheduled=master.start) if master.rules else master)

        def saveEvent_span_commit_error_(self, event, span, _commit, _error):  # noqa: N802
            if store.fail:
                return False, types.SimpleNamespace(localizedDescription=lambda: store.fail)
            store.saves.append((event.title(), span))
            if event.ident is None:
                store.count += 1
                event.ident = f"E{store.count}"
                store.series[event.ident] = event
            elif event.rules and span == THIS:
                store.moved[(event.ident, event.scheduled.ts)] = event.copy(detached=True)
            else:
                store.series[event.ident] = event.copy(scheduled=None)
            return True, None

        def removeEvent_span_commit_error_(self, event, span, _commit, _error):  # noqa: N802
            store.removals.append((event.ident, span))
            if event.rules and span == THIS:
                store.skipped.add((event.ident, event.scheduled.ts))
            else:
                del store.series[event.ident]
            return True, None

    ek = types.SimpleNamespace(
        EKEventStore=Store,
        EKEntityTypeEvent=0,
        EKAuthorizationStatusNotDetermined=NOT_DETERMINED,
        EKAuthorizationStatusFullAccess=FULL,
        EKAuthorizationStatusWriteOnly=WRITE_ONLY,
        EKSpanThisEvent=THIS,
        EKSpanFutureEvents=FUTURE,
        EKEventAvailabilityFree=FREE,
        EKEventStatusCanceled=CANCELED,
        EKEvent=types.SimpleNamespace(eventWithEventStore_=lambda _store: _Event()),
        EKRecurrenceFrequencyDaily=0,
        EKRecurrenceFrequencyWeekly=1,
        EKRecurrenceFrequencyMonthly=2,
        EKRecurrenceFrequencyYearly=3,
        EKRecurrenceEnd=types.SimpleNamespace(
            recurrenceEndWithOccurrenceCount_=lambda n: ("count", n),
            recurrenceEndWithEndDate_=lambda d: ("until", d),
        ),
        EKRecurrenceDayOfWeek=types.SimpleNamespace(dayOfWeek_=lambda n: n),
        EKRecurrenceRule=types.SimpleNamespace(alloc=lambda: types.SimpleNamespace(**{_NEW_RULE: _rule})),
    )
    return ek, store


@pytest.fixture
def mac(monkeypatch):
    """Installs a fake EventKit on a "Mac", with no CalDAV account."""
    monkeypatch.setattr(sys, "platform", "darwin")
    foundation = types.SimpleNamespace(
        NSDate=types.SimpleNamespace(dateWithTimeIntervalSince1970_=_Date),
        NSTimeZone=types.SimpleNamespace(timeZoneWithName_=_Zone),
    )
    monkeypatch.setitem(sys.modules, "Foundation", foundation)

    def install(**options):
        ek, store = _framework(**options)
        monkeypatch.setitem(sys.modules, "EventKit", ek)
        return store

    return install


def _put(store, **values):
    """An event saved in the Calendar app."""
    store.count += 1
    event = _Event(ident=f"E{store.count}", cal=store.cals[0], **values)
    store.series[event.ident] = event
    return event


def test_the_calendars_come_with_their_account_and_the_default(mac):
    mac()
    found, problems = service.calendars()
    assert [(c.id, c.name, c.account, c.writable, c.default) for c in found] == [
        ("mac:work", "Work", "iCloud", True, True),
        ("mac:home", "Home", "iCloud", True, False),
        ("mac:hol", "Holidays", "Other", False, False),
    ]
    assert problems == []


def test_events_read_as_the_calendar_app_has_them(mac):
    store = mac()
    _put(
        store,
        _title="Review",
        start=_at("2026-10-13T12:00"),
        end=_at("2026-10-13T13:00"),
        zone=_Zone("Europe/Madrid"),
        people=[_Participant("Juan Pérez", "juan@example.com"), _Participant(None, "lu@example.com")],
        link=types.SimpleNamespace(absoluteString=lambda: "https://meet.example.com/abc"),
    )
    _put(store, _title="Trip", start=_at("2026-10-12T00:00"), end=_at("2026-10-14T23:59:59"), all_day=True)
    _put(store, _title="Focus", start=_at("2026-10-13T15:00"), end=_at("2026-10-13T16:00"), _availability=FREE)
    trip, review, focus = service.events("2026-10-13").events
    assert (trip.start, trip.end, trip.all_day) == ("2026-10-12", "2026-10-14", True)
    assert (review.id, review.start, review.timezone, review.attendees, review.url) == (
        "mac:E1",
        "2026-10-13T12:00-03:00",
        "Europe/Madrid",
        ["Juan Pérez", "lu@example.com"],
        "https://meet.example.com/abc",
    )
    assert not focus.busy


def test_a_new_event_goes_to_the_default_calendar(mac):
    store = mac()
    made = service.create("Dentist", "2026-10-12T15:00", location="Av. Corrientes 1234")
    assert (made.calendar, made.start, made.end, made.location) == (
        "Work",
        "2026-10-12T15:00-03:00",
        "2026-10-12T16:00-03:00",
        "Av. Corrientes 1234",
    )
    saved = store.series["E1"]
    assert saved.zone.name() == ZONE and saved.notes() is None and store.saves == [("Dentist", THIS)]


def test_an_all_day_event_is_saved_as_whole_days(mac):
    store = mac()
    made = service.create("Trip", "2026-10-14", "2026-10-16", calendar="Home")
    saved = store.series["E1"]
    assert saved.all_day and saved.zone is None and saved.start.ts == _at("2026-10-14T00:00").ts
    assert (made.start, made.end, made.calendar) == ("2026-10-14", "2026-10-16", "Home")


def test_a_calendar_that_cant_be_changed_is_refused(mac):
    mac()
    with pytest.raises(CalendarError, match="can't be changed"):
        service.create("Party", "2026-10-12T20:00", calendar="Holidays")


def test_a_repeating_event_gets_its_rule(mac):
    store = mac()
    service.create("Gym", "2026-10-12T19:00", repeat="FREQ=WEEKLY;INTERVAL=2;BYDAY=MO,SU;UNTIL=20261231")
    rule = store.series["E1"].rules[0]
    assert (rule.freq, rule.interval, rule.days) == (1, 2, [2, 1])
    assert rule.end[0] == "until" and rule.end[1].ts == _at("2026-12-31T23:59:59").ts


def test_one_occurrence_or_the_series_changes(mac):
    store = mac()
    made = service.create("Standup", "2026-10-12T09:00", repeat="FREQ=WEEKLY;COUNT=3")
    occurrences = service.events("2026-10-01", "2026-11-30").events
    assert [e.occurrence for e in occurrences] == [
        "2026-10-12T09:00-03:00",
        "2026-10-19T09:00-03:00",
        "2026-10-26T09:00-03:00",
    ]
    moved = service.update(made.id, "2026-10-19T09:00", start="2026-10-19T11:00")
    assert (moved.start, moved.end, moved.occurrence) == (
        "2026-10-19T11:00-03:00",
        "2026-10-19T12:00-03:00",
        "2026-10-19T09:00-03:00",
    )
    service.update(made.id, title="Daily standup")
    assert store.saves[-2:] == [("Standup", THIS), ("Daily standup", FUTURE)]
    service.delete(made.id, "2026-10-26T09:00")
    assert store.removals == [("E1", THIS)]
    assert [(e.title, e.start) for e in service.events("2026-10-01", "2026-11-30").events] == [
        ("Daily standup", "2026-10-12T09:00-03:00"),
        ("Standup", "2026-10-19T11:00-03:00"),
    ]
    service.delete(made.id)
    assert store.removals[-1] == ("E1", FUTURE) and service.events("2026-10-01", "2026-11-30").events == []


def test_an_occurrence_of_a_single_event_is_refused(mac):
    store = mac()
    _put(store, _title="Dentist", start=_at("2026-10-12T15:00"), end=_at("2026-10-12T16:00"))
    with pytest.raises(CalendarError, match="doesn't repeat"):
        service.delete("mac:E1", "2026-10-12T15:00")
    with pytest.raises(CalendarError, match="no event with that id"):
        service.event("mac:E9")


def test_a_failed_save_says_why(mac):
    store = mac()
    store.fail = "The calendar is read-only."
    with pytest.raises(CalendarError, match=r"couldn't save the event: The calendar is read-only\."):
        service.create("Dentist", "2026-10-12T15:00")


def test_the_first_use_asks_for_access(mac):
    store = mac(status=NOT_DETERMINED)
    assert service.calendars()[0] and store.asked == [True]


def test_denied_access_says_where_to_allow_it(mac):
    mac(status=NOT_DETERMINED, grant=False)
    with pytest.raises(CalendarError, match="Privacy & Security → Calendars"):
        service.calendars()
    mac(status=DENIED)
    with pytest.raises(CalendarError, match="No access to the Mac's calendars"):
        service.events("2026-10-12")
    mac(status=WRITE_ONLY)
    with pytest.raises(CalendarError, match="Allow Full Access"):
        service.events("2026-10-12")


def test_without_access_the_caldav_account_still_answers(mac, dav, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    mac(status=DENIED)
    found, problems = service.calendars()
    assert sorted(c.name for c in found) == ["Home", "Work"]
    assert len(problems) == 1 and problems[0].startswith("Mac: No access to the Mac's calendars")


def test_off_a_mac_without_an_account_nothing_is_set_up(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    with pytest.raises(CalendarError, match="No calendars are set up"):
        service.calendars()
