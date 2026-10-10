"""The calendars in the Mac's Calendar app, with EventKit (pyobjc): whatever accounts the Mac has (iCloud,
Google, Exchange…) and its local calendars. EventKit expands repeating events itself. The first use asks
macOS for access, which shows the user a prompt; once they decide, macOS remembers it."""

import importlib
import sys
import threading
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from lo9i_calendar import times
from lo9i_calendar.model import AllDay, Calendar, CalendarError, Changes, Event, NewEvent, Repeat, Timed, Timing

KEY = "mac"
# How long the first use waits for the user to answer macOS's access prompt.
ACCESS_PROMPT_SECONDS = 120
# How far from when it was scheduled an occurrence is looked for: moving one changes its dates, not that.
_MOVED_DAYS = 7
_NO_ACCESS = (
    "No access to the Mac's calendars. Allow it in System Settings → Privacy & Security → Calendars "
    "(Full Access), for the app lo9i was started from."
)


class Mac:
    key = KEY
    name = "Mac"

    # --- Reading ---

    def calendars(self) -> list[Calendar]:
        ek, store = _store()
        default = store.defaultCalendarForNewEvents()
        default_id = default.calendarIdentifier() if default is not None else None
        calendars = store.calendarsForEntityType_(ek.EKEntityTypeEvent)
        return [_calendar(c, c.calendarIdentifier() == default_id) for c in calendars]

    def events(self, start: datetime, end: datetime, calendar_ids: list[str] | None) -> list[Event]:
        ek, store = _store()
        cals = None
        if calendar_ids is not None:
            calendars = store.calendarsForEntityType_(ek.EKEntityTypeEvent)
            cals = [c for c in calendars if c.calendarIdentifier() in calendar_ids]
            if not cals:
                return []
        tz = times.user_zone()
        predicate = store.predicateForEventsWithStartDate_endDate_calendars_(_ns(start), _ns(end), cals)
        return [_event(ek, e, tz) for e in store.eventsMatchingPredicate_(predicate) or []]

    def event(self, event_id: str, occurrence: date | datetime | None) -> Event:
        ek, store = _store()
        tz = times.user_zone()
        return _event(ek, _find(ek, store, event_id, occurrence, tz), tz, series=occurrence is None)

    # --- Writing ---

    def create(self, calendar_id: str, new: NewEvent) -> Event:
        ek, store = _store()
        cal = store.calendarWithIdentifier_(calendar_id) if calendar_id else store.defaultCalendarForNewEvents()
        if cal is None:
            raise CalendarError("The Mac has no such calendar: list_calendars gives their ids.")
        if not cal.allowsContentModifications():
            raise CalendarError(f"The calendar {cal.title()} can't be changed: pick another one.")
        event = ek.EKEvent.eventWithEventStore_(store)
        event.setCalendar_(cal)
        event.setTitle_(new.title)
        _set_timing(event, new.timing)
        event.setLocation_(new.location or None)
        event.setNotes_(new.notes or None)
        if new.repeat:
            event.addRecurrenceRule_(_rule(ek, new.repeat, new.timing))
        _save(store, event, ek.EKSpanThisEvent)
        return _event(ek, event, times.user_zone(), series=True)

    def update(self, event_id: str, occurrence: date | datetime | None, changes: Changes) -> Event:
        ek, store = _store()
        tz = times.user_zone()
        event = _find(ek, store, event_id, occurrence, tz)
        if changes.title is not None:
            event.setTitle_(changes.title)
        if changes.location is not None:
            event.setLocation_(changes.location or None)
        if changes.notes is not None:
            event.setNotes_(changes.notes or None)
        if changes.timing is not None:
            _set_timing(event, changes.timing)
        _save(store, event, _span(ek, event, occurrence))
        return _event(ek, event, tz, series=occurrence is None)

    def delete(self, event_id: str, occurrence: date | datetime | None) -> None:
        ek, store = _store()
        event = _find(ek, store, event_id, occurrence, times.user_zone())
        ok, error = store.removeEvent_span_commit_error_(event, _span(ek, event, occurrence), True, None)
        if not ok:
            raise CalendarError(f"Calendar couldn't delete the event: {_describe(error)}")


def _store():
    """EventKit and an event store with access. Raises CalendarError off a Mac or without access."""
    if sys.platform != "darwin":
        raise CalendarError("The Calendar app's calendars are only on a Mac.")
    ek = _framework("EventKit")
    _require_access(ek)
    # A new store each time sees changes made in the Calendar app meanwhile.
    return ek, ek.EKEventStore.alloc().init()


def _framework(name: str):
    """A macOS framework through pyobjc, installed only on a Mac. Its names are made at run time, so it's
    imported by name: type checkers can't see them anyway."""
    return importlib.import_module(name)


def _require_access(ek) -> None:
    status = ek.EKEventStore.authorizationStatusForEntityType_(ek.EKEntityTypeEvent)
    if status == ek.EKAuthorizationStatusFullAccess:
        return
    if status == ek.EKAuthorizationStatusWriteOnly:
        raise CalendarError(
            "The assistant may only add events to the Mac's calendars, not read them. Allow Full Access in "
            "System Settings → Privacy & Security → Calendars."
        )
    if status != ek.EKAuthorizationStatusNotDetermined or not _ask_access(ek):
        raise CalendarError(_NO_ACCESS)


def _ask_access(ek) -> bool:
    """macOS answers on another thread once the user picks Allow or Don't Allow."""
    answered = threading.Event()
    granted: list[bool] = []

    def done(ok: bool, _error: object) -> None:
        granted.append(bool(ok))
        answered.set()

    store = ek.EKEventStore.alloc().init()
    if hasattr(store, "requestFullAccessToEventsWithCompletion_"):  # macOS 14 and later
        store.requestFullAccessToEventsWithCompletion_(done)
    else:
        store.requestAccessToEntityType_completion_(ek.EKEntityTypeEvent, done)
    return answered.wait(ACCESS_PROMPT_SECONDS) and granted[0]


def _find(ek, store, event_id: str, occurrence: date | datetime | None, tz: ZoneInfo):
    """The event, the first of its series for a repeating one; or its occurrence scheduled at `occurrence`."""
    event = store.eventWithIdentifier_(event_id)
    if event is None:
        raise CalendarError("There's no event with that id: list_events gives the ids.")
    if occurrence is None:
        return event
    if not _repeats(event):
        raise CalendarError("That event doesn't repeat: leave occurrence out.")
    wanted = times.utc_key(occurrence, tz)
    moment = occurrence if isinstance(occurrence, datetime) else times.midnight(occurrence, tz)
    window = timedelta(days=_MOVED_DAYS)
    predicate = store.predicateForEventsWithStartDate_endDate_calendars_(
        _ns(moment - window), _ns(moment + window + timedelta(days=1)), [event.calendar()]
    )
    for found in store.eventsMatchingPredicate_(predicate) or []:
        if found.eventIdentifier() == event_id and times.utc_key(_scheduled(found, tz), tz) == wanted:
            return found
    raise CalendarError(f"That event has no occurrence at {times.show(occurrence, tz)}: list_events gives them.")


def _span(ek, event, occurrence: date | datetime | None) -> int:
    """This occurrence only, or the whole series: the first occurrence and the ones after it."""
    return ek.EKSpanThisEvent if occurrence is not None or not _repeats(event) else ek.EKSpanFutureEvents


def _save(store, event, span: int) -> None:
    ok, error = store.saveEvent_span_commit_error_(event, span, True, None)
    if not ok:
        raise CalendarError(f"Calendar couldn't save the event: {_describe(error)}")


def _describe(error) -> str:
    return str(error.localizedDescription()) if error is not None else "no reason given"


def _repeats(event) -> bool:
    return bool(event.hasRecurrenceRules()) or bool(event.isDetached())


def _calendar(cal, default: bool) -> Calendar:
    source = cal.source()
    account = str(source.title()) if source is not None and source.title() else "Mac"
    writable = bool(cal.allowsContentModifications())
    return Calendar(f"{KEY}:{cal.calendarIdentifier()}", str(cal.title()), account, writable, default)


def _event(ek, event, tz: ZoneInfo, series: bool = False) -> Event:
    all_day = bool(event.isAllDay())
    start, end = _from_ns(event.startDate(), tz), _from_ns(event.endDate(), tz)
    if all_day:
        # EventKit ends an all-day event at 23:59:59 of its last day, or at midnight after it.
        start, end = start.date(), max(start.date(), (end - timedelta(seconds=1)).date())
    recurring = _repeats(event)
    zone = event.timeZone()
    zone_name = str(zone.name()) if zone is not None and not all_day else ""
    cal = event.calendar()
    url = event.URL()
    return Event(
        id=f"{KEY}:{event.eventIdentifier()}",
        calendar_id=f"{KEY}:{cal.calendarIdentifier()}",
        calendar=str(cal.title()),
        title=str(event.title() or ""),
        start=times.show(start, tz),
        end=times.show(end, tz),
        all_day=all_day,
        location=str(event.location() or ""),
        notes=str(event.notes() or ""),
        recurring=recurring,
        occurrence=times.show(_scheduled(event, tz), tz) if recurring and not series else "",
        timezone=zone_name if zone_name != tz.key else "",
        busy=event.availability() != ek.EKEventAvailabilityFree and event.status() != ek.EKEventStatusCanceled,
        attendees=[_attendee(p) for p in event.attendees() or []],
        url=str(url.absoluteString()) if url is not None else "",
    )


def _scheduled(event, tz: ZoneInfo) -> date | datetime:
    """When the occurrence was scheduled in its series, before any move: a date for an all-day one."""
    moment = _from_ns(event.occurrenceDate() or event.startDate(), tz)
    return moment.date() if event.isAllDay() else moment


def _attendee(participant) -> str:
    if participant.name():
        return str(participant.name())
    url = participant.URL()
    return str(url.resourceSpecifier()) if url is not None else ""


def _set_timing(event, timing: Timing) -> None:
    if isinstance(timing, AllDay):
        event.setAllDay_(True)
        event.setTimeZone_(None)
        event.setStartDate_(_ns(times.midnight(timing.start, times.user_zone())))
        # Any time in the last day ends an all-day event on that day; noon can't fall on the day before or after.
        event.setEndDate_(_ns(times.midnight(timing.end, times.user_zone()) + timedelta(hours=12)))
        return
    event.setAllDay_(False)
    event.setTimeZone_(_framework("Foundation").NSTimeZone.timeZoneWithName_(timing.zone.key))
    event.setStartDate_(_ns(timing.start))
    event.setEndDate_(_ns(timing.end))


def _rule(ek, repeat: Repeat, timing: Timing):
    frequencies = {
        "DAILY": ek.EKRecurrenceFrequencyDaily,
        "WEEKLY": ek.EKRecurrenceFrequencyWeekly,
        "MONTHLY": ek.EKRecurrenceFrequencyMonthly,
        "YEARLY": ek.EKRecurrenceFrequencyYearly,
    }
    end = None
    if repeat.count:
        end = ek.EKRecurrenceEnd.recurrenceEndWithOccurrenceCount_(repeat.count)
    elif repeat.until:
        zone = timing.zone if isinstance(timing, Timed) else times.user_zone()
        end = ek.EKRecurrenceEnd.recurrenceEndWithEndDate_(_ns(times.end_of_day(repeat.until, zone)))
    # EKWeekday counts from Sunday, 1, to Saturday, 7.
    days = [ek.EKRecurrenceDayOfWeek.dayOfWeek_((times.WEEKDAYS.index(d) + 1) % 7 + 1) for d in repeat.days]
    return ek.EKRecurrenceRule.alloc().initRecurrenceWithFrequency_interval_daysOfTheWeek_daysOfTheMonth_monthsOfTheYear_weeksOfTheYear_daysOfTheYear_setPositions_end_(  # noqa: E501
        frequencies[repeat.freq], repeat.interval, days or None, None, None, None, None, None, end
    )


def _ns(moment: datetime):
    return _framework("Foundation").NSDate.dateWithTimeIntervalSince1970_(moment.timestamp())


def _from_ns(value, tz: ZoneInfo) -> datetime:
    return datetime.fromtimestamp(value.timeIntervalSince1970(), tz)
