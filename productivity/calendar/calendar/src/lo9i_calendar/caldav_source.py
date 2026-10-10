"""Calendars on a CalDAV server (iCloud, Fastmail, Nextcloud…), with the caldav library. Events are iCalendar
data, read and written whole; repeating ones are expanded here, so every server gives the same occurrences."""

import time
import uuid
from collections.abc import Coroutine, Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import icalendar
import recurring_ical_events
from caldav.calendarobjectresource import CalendarObjectResource
from caldav.collection import Calendar as DavCalendar
from caldav.davclient import DAVClient
from caldav.lib import error as dav_error
from caldav.lib.url import URL

from lo9i_calendar import times
from lo9i_calendar.model import AllDay, Calendar, CalendarError, Changes, Event, NewEvent, Repeat, Timed, Timing

KEY = "caldav"
# How long the list of calendars is kept before it's read again.
CALENDARS_SECONDS = 300
_TIMEOUT_SECONDS = 30
# Parts of a series that an occurrence moved on its own doesn't keep.
_SERIES_ONLY = ("RRULE", "RDATE", "EXDATE", "EXRULE")


class CalDav:
    key = KEY

    def __init__(self, url: str, username: str, password: str) -> None:
        self.url, self._username, self._password = url.strip(), username.strip(), password
        host = urlsplit(self.url if "://" in self.url else f"https://{self.url}").hostname or self.url
        self.name = f"CalDAV ({host})"
        self._client: DAVClient | None = None
        self._calendars: tuple[float, list[DavCalendar]] | None = None

    # --- Reading ---

    def calendars(self) -> list[Calendar]:
        with self._errors():
            return [self._calendar(c) for c in self._dav_calendars()]

    def events(self, start: datetime, end: datetime, calendar_ids: list[str] | None) -> list[Event]:
        tz = times.user_zone()
        found: list[Event] = []
        with self._errors():
            for cal in self._dav_calendars():
                if calendar_ids is not None and _path(cal.url) not in calendar_ids:
                    continue
                for obj in _sync(cal.search(event=True, start=start, end=end, expand=False)):
                    found.extend(self._occurrences(cal, obj, start, end, tz))
        return found

    def _occurrences(self, cal, obj, start: datetime, end: datetime, tz: ZoneInfo) -> list[Event]:
        """The event's occurrences in the range, or none when its data can't be read: one broken event
        shouldn't hide the rest."""
        try:
            ical = icalendar.Calendar.from_ical(obj.data)
            recurring = _recurring_uids(ical)
            between = recurring_ical_events.of(ical).between(start, end)
        except ValueError:  # icalendar's and recurring_ical_events' errors for data they can't read
            return []
        return [self._event(cal, obj.url, comp, str(comp.get("UID")) in recurring, tz) for comp in between]

    def event(self, event_id: str, occurrence: date | datetime | None) -> Event:
        tz = times.user_zone()
        with self._errors():
            cal, obj = self._object(event_id)
            ical = icalendar.Calendar.from_ical(obj.data)
            if occurrence is None:
                return self._event(cal, obj.url, _master(ical), bool(_recurring_uids(ical)), tz, series=True)
            return self._event(cal, obj.url, _occurrence(ical, occurrence, tz), True, tz)

    # --- Writing ---

    def create(self, calendar_id: str, new: NewEvent) -> Event:
        with self._errors():
            cal = self._find_calendar(calendar_id)
            comp = icalendar.Event()
            comp.add("uid", str(uuid.uuid4()))
            comp.add("dtstamp", datetime.now(UTC))
            comp.add("summary", new.title)
            _set_timing(comp, new.timing)
            for key, text in (("location", new.location), ("description", new.notes)):
                _set_text(comp, key, text)
            if new.repeat:
                comp.add("rrule", _rrule(new.repeat, new.timing))
            ical = icalendar.Calendar()
            ical.add("prodid", "-//lo9i//calendar//EN")
            ical.add("version", "2.0")
            ical.add_component(comp)
            ical.add_missing_timezones()
            obj = _sync(cal.add_event(ical.to_ical().decode()))
            return self._event(cal, obj.url, comp, new.repeat is not None, times.user_zone(), series=True)

    def update(self, event_id: str, occurrence: date | datetime | None, changes: Changes) -> Event:
        tz = times.user_zone()
        with self._errors():
            cal, obj = self._object(event_id)
            ical = icalendar.Calendar.from_ical(obj.data)
            comp = _master(ical) if occurrence is None else _override(ical, occurrence, tz)
            for key, text in (("summary", changes.title), ("location", changes.location)):
                if text is not None:
                    _set_text(comp, key, text)
            if changes.notes is not None:
                _set_text(comp, "description", changes.notes)
            if changes.timing is not None:
                moved_from = comp.decoded("DTSTART")
                _set_timing(comp, changes.timing)
                if occurrence is None:
                    _move_exceptions(ical, moved_from, comp.decoded("DTSTART"), tz)
            _touch(comp)
            self._save(obj, ical)
            return self._event(cal, obj.url, comp, bool(_recurring_uids(ical)), tz, series=occurrence is None)

    def delete(self, event_id: str, occurrence: date | datetime | None) -> None:
        tz = times.user_zone()
        with self._errors():
            _, obj = self._object(event_id)
            if occurrence is None:
                obj.delete()
                return
            ical = icalendar.Calendar.from_ical(obj.data)
            when = _occurrence(ical, occurrence, tz).decoded("RECURRENCE-ID")
            master = _master(ical)
            for comp in _overrides(ical, when, tz):
                ical.subcomponents.remove(comp)
            master.add("exdate", _like(master.decoded("DTSTART"), when, tz))
            _touch(master)
            self._save(obj, ical)

    # --- The server ---

    def _dav_calendars(self) -> list[DavCalendar]:
        """The calendars that hold events (not task lists), kept a few minutes."""
        if self._calendars and time.monotonic() - self._calendars[0] < CALENDARS_SECONDS:
            return self._calendars[1]
        if self._client is None:
            self._client = DAVClient(
                url=self.url, username=self._username, password=self._password, timeout=_TIMEOUT_SECONDS
            )
        everything = _sync(self._client.principal().calendars())
        found = [c for c in everything if "VEVENT" in _sync(c.get_supported_components())]
        self._calendars = (time.monotonic(), found)
        return found

    def _find_calendar(self, calendar_id: str) -> DavCalendar:
        found = self._dav_calendars()
        cal = next((c for c in found if _path(c.url) == calendar_id), None) if calendar_id else next(iter(found), None)
        if cal is None:
            raise CalendarError(f"{self.name} has no such calendar: list_calendars gives their ids.")
        return cal

    def _object(self, event_id: str) -> tuple[DavCalendar, CalendarObjectResource]:
        """The event's calendar and its iCalendar object, from its path on the server."""
        cal = next((c for c in self._dav_calendars() if event_id.startswith(_path(c.url))), None)
        if cal is None:
            raise CalendarError("There's no event with that id: list_events gives the ids.")
        return cal, cal.event_by_url(event_id)

    def _save(self, obj: CalendarObjectResource, ical: icalendar.Calendar) -> None:
        ical.add_missing_timezones()
        obj.data = ical.to_ical().decode()
        # Saved whole, as edited here: the library's own merging of occurrences isn't wanted.
        obj.save(increase_seqno=False, only_this_recurrence=False)

    @contextmanager
    def _errors(self) -> Iterator[None]:
        """The server's and the network's errors, as messages that say what to do."""
        try:
            yield
        except dav_error.AuthorizationError:
            raise CalendarError(
                f"{self.name} refused the username or password. Most providers need an app password: check it, "
                "then install the calendar plugin again with the right one."
            ) from None
        except dav_error.NotFoundError:
            raise CalendarError("There's no event with that id any more: list_events gives the ids.") from None
        except (dav_error.ETagMismatchError, dav_error.ScheduleTagMismatchError):
            raise CalendarError("The event changed on the server meanwhile: read it again and retry.") from None
        except dav_error.DAVError as e:
            raise CalendarError(f"{self.name} answered with an error: {e}") from None
        except OSError as e:
            raise CalendarError(f"{self.name} couldn't be reached: {e}") from None

    def _calendar(self, cal: DavCalendar) -> Calendar:
        return Calendar(f"{KEY}:{_path(cal.url)}", _name(cal), self.name)

    def _event(self, cal, url, comp, recurring: bool, tz: ZoneInfo, series: bool = False) -> Event:
        start = comp.decoded("DTSTART")
        end = _end(comp, start)
        all_day = not isinstance(start, datetime)
        own_zone = getattr(start, "tzinfo", None)
        when = comp.decoded("RECURRENCE-ID") if "RECURRENCE-ID" in comp else start
        return Event(
            id=f"{KEY}:{_path(url)}",
            calendar_id=f"{KEY}:{_path(cal.url)}",
            calendar=_name(cal),
            title=str(comp.get("SUMMARY", "")),
            start=times.show(start, tz),
            end=times.show(end - timedelta(days=1) if all_day and end > start else end, tz),
            all_day=all_day,
            location=str(comp.get("LOCATION", "")),
            notes=str(comp.get("DESCRIPTION", "")),
            recurring=recurring,
            occurrence=times.show(when, tz) if recurring and not series else "",
            timezone=own_zone.key if isinstance(own_zone, ZoneInfo) and own_zone.key != tz.key else "",
            busy=str(comp.get("TRANSP", "")).upper() != "TRANSPARENT"
            and str(comp.get("STATUS", "")).upper() != "CANCELLED",
            attendees=[_attendee(a) for a in _list(comp.get("ATTENDEE"))],
            url=str(comp.get("URL", "")),
        )


def _path(url) -> str:
    return URL.objectify(url).path


def _name(cal: DavCalendar) -> str:
    return _sync(cal.get_display_name()) or _path(cal.url).rstrip("/").rsplit("/", 1)[-1]


def _sync[T](value: T | Coroutine[Any, Any, T]) -> T:
    """What a call of the sync client returned: caldav types its calls as returning a value or, for its async
    client, a coroutine."""
    assert not isinstance(value, Coroutine)
    return value


def _list(value) -> list:
    return [] if value is None else value if isinstance(value, list) else [value]


def _attendee(value) -> str:
    name = value.params.get("CN", "") if hasattr(value, "params") else ""
    return str(name or str(value).removeprefix("mailto:").removeprefix("MAILTO:"))


def _end(comp, start: date | datetime) -> date | datetime:
    if "DTEND" in comp:
        return comp.decoded("DTEND")
    if "DURATION" in comp:
        return start + comp.decoded("DURATION")
    return start if isinstance(start, datetime) else start + timedelta(days=1)


def _events(ical: icalendar.Calendar) -> list:
    return [c for c in ical.subcomponents if c.name == "VEVENT"]


def _recurring_uids(ical: icalendar.Calendar) -> set[str]:
    return {str(c.get("UID")) for c in _events(ical) if {"RRULE", "RDATE", "RECURRENCE-ID"} & set(c.keys())}


def _master(ical: icalendar.Calendar):
    events = _events(ical)
    if not events:
        raise CalendarError("That id isn't an event.")
    return next((c for c in events if "RECURRENCE-ID" not in c), events[0])


def _occurrence(ical: icalendar.Calendar, when: date | datetime, tz: ZoneInfo):
    """The occurrence scheduled at `when` (a copy, as recurring_ical_events computes it)."""
    if not _recurring_uids(ical):
        raise CalendarError("That event doesn't repeat: leave occurrence out.")
    wanted = times.utc_key(when, tz)
    moment = when if isinstance(when, datetime) else times.midnight(when, tz)
    # A week either way finds it even when it was moved, which changes its start but not its RECURRENCE-ID.
    window = recurring_ical_events.of(ical).between(moment - timedelta(days=7), moment + timedelta(days=8))
    for comp in window:
        if "RECURRENCE-ID" in comp and times.utc_key(comp.decoded("RECURRENCE-ID"), tz) == wanted:
            return comp
    raise CalendarError(f"That event has no occurrence at {times.show(when, tz)}: list_events gives them.")


def _overrides(ical: icalendar.Calendar, when: date | datetime, tz: ZoneInfo) -> list:
    """The components that already change the occurrence at `when`."""
    key = times.utc_key(when, tz)
    return [c for c in _events(ical) if "RECURRENCE-ID" in c and times.utc_key(c.decoded("RECURRENCE-ID"), tz) == key]


def _override(ical: icalendar.Calendar, when: date | datetime, tz: ZoneInfo):
    """A component of its own for the occurrence at `when`, in place of any the event had for it."""
    # A copy of its own, so that changing it leaves the series alone.
    occurrence = icalendar.Event.from_ical(_occurrence(ical, when, tz).to_ical())
    scheduled = occurrence.decoded("RECURRENCE-ID")
    for comp in _overrides(ical, scheduled, tz):
        ical.subcomponents.remove(comp)
    for key in _SERIES_ONLY:
        occurrence.pop(key, None)
    occurrence.pop("RECURRENCE-ID", None)
    occurrence.add("recurrence-id", _like(_master(ical).decoded("DTSTART"), scheduled, tz))
    ical.add_component(occurrence)
    return occurrence


def _like(series_start: date | datetime, when: date | datetime, tz: ZoneInfo) -> date | datetime:
    """`when` written the way the series' DTSTART is (a date, a floating time or a time in its zone), as
    RECURRENCE-ID and EXDATE must be."""
    if not isinstance(series_start, datetime):
        return when.date() if isinstance(when, datetime) else when
    moment = _moment(when, tz)
    if series_start.tzinfo is None:
        return moment.astimezone(tz).replace(tzinfo=None)
    return moment.astimezone(series_start.tzinfo)


def _move_exceptions(ical: icalendar.Calendar, old: date | datetime, new: date | datetime, tz: ZoneInfo) -> None:
    """Move the series' deleted (EXDATE) and changed (RECURRENCE-ID) occurrences with its start, as calendar
    apps do, so they still name occurrences of it."""
    shift = _moment(new, tz) - _moment(old, tz)
    if not shift and type(old) is type(new):
        return
    master = _master(ical)
    deleted = [d.dt for prop in _list(master.pop("EXDATE", None)) for d in prop.dts]
    for when in deleted:
        master.add("exdate", _like(new, _moment(when, tz) + shift, tz))
    for comp in _events(ical):
        if "RECURRENCE-ID" in comp:
            when = comp.decoded("RECURRENCE-ID")
            comp.pop("RECURRENCE-ID")
            comp.add("recurrence-id", _like(new, _moment(when, tz) + shift, tz))


def _moment(value: date | datetime, tz: ZoneInfo) -> datetime:
    if not isinstance(value, datetime):
        return times.midnight(value, tz)
    return value.replace(tzinfo=tz) if value.tzinfo is None else value


def _set_timing(comp, timing: Timing) -> None:
    for key in ("DTSTART", "DTEND", "DURATION"):
        comp.pop(key, None)
    if isinstance(timing, AllDay):
        comp.add("dtstart", timing.start)
        comp.add("dtend", timing.end + timedelta(days=1))
    else:
        comp.add("dtstart", timing.start.astimezone(timing.zone))
        comp.add("dtend", timing.end.astimezone(timing.zone))


def _set_text(comp, key: str, text: str) -> None:
    comp.pop(key.upper(), None)
    if text:
        comp.add(key, text)


def _touch(comp) -> None:
    """Mark a changed component as a newer version, for the clients that sync it."""
    comp["SEQUENCE"] = int(comp.get("SEQUENCE", 0)) + 1
    for key in ("DTSTAMP", "LAST-MODIFIED"):
        comp.pop(key, None)
        comp.add(key.lower(), datetime.now(UTC))


def _rrule(repeat: Repeat, timing: Timing) -> dict:
    rule: dict = {"freq": repeat.freq}
    if repeat.interval > 1:
        rule["interval"] = repeat.interval
    if repeat.count:
        rule["count"] = repeat.count
    if repeat.until and isinstance(timing, Timed):
        # A timed event's UNTIL is a time in UTC.
        rule["until"] = times.end_of_day(repeat.until, timing.zone).astimezone(UTC)
    elif repeat.until:
        rule["until"] = repeat.until
    if repeat.days:
        rule["byday"] = list(repeat.days)
    return rule
