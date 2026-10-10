"""The user's calendars across their sources: the CalDAV account given in the plugin's fields, and on a Mac
the Calendar app's. A source that fails doesn't hide the others: what it said comes back as a problem."""

import os
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from functools import cache
from zoneinfo import ZoneInfo

from lo9i_calendar import times
from lo9i_calendar.caldav_source import CalDav
from lo9i_calendar.mac_source import Mac
from lo9i_calendar.model import AllDay, Calendar, CalendarError, Changes, Event, NewEvent, Source, Timed, Timing

# The most events one call returns.
MAX_EVENTS = 200
_NOTHING_SET_UP = (
    "No calendars are set up: the calendar plugin reads the Calendar app on a Mac, or a CalDAV account "
    "entered when installing it. Install it again from Plugins with the CalDAV server, username and app password."
)


@dataclass(frozen=True)
class Listing:
    events: list[Event]
    # Sources that couldn't be read, with why.
    problems: list[str] = field(default_factory=list)
    # There were more than MAX_EVENTS.
    more: bool = False


@dataclass
class _Busy:
    start: datetime
    end: datetime
    titles: list[str]


@cache
def sources() -> list[Source]:
    """The CalDAV account when its fields are filled in, and the Mac's calendars on a Mac."""
    found: list[Source] = []
    url = os.environ.get("CALDAV_URL", "").strip()
    if url:
        found.append(CalDav(url, os.environ.get("CALDAV_USERNAME", ""), os.environ.get("CALDAV_PASSWORD", "")))
    if sys.platform == "darwin":
        found.append(Mac())
    return found


# --- Reading ---


def calendars() -> tuple[list[Calendar], list[str]]:
    """Every source's calendars, and the problems of those that couldn't be read."""
    found: list[Calendar] = []
    problems: list[str] = []
    for source in _all():
        try:
            found.extend(source.calendars())
        except CalendarError as e:
            problems.append(f"{source.name}: {e}")
    if not found and problems:
        raise CalendarError(" ".join(problems))
    return found, problems


def events(start: str = "", end: str = "", calendar: str = "", query: str = "") -> Listing:
    """Events from `start` to `end` (times.span), in the calendars named `calendar` (all when empty), whose
    title, location or notes have `query`; each occurrence of a repeating event on its own."""
    begin, finish = times.span(start, end, times.user_zone())
    wanted = _named(calendar) if calendar.strip() else None
    found: list[Event] = []
    problems: list[str] = []
    for source in _all():
        ids = None if wanted is None else [_native(c.id)[1] for c in wanted if c.id.startswith(f"{source.key}:")]
        if ids == []:
            continue
        try:
            found.extend(source.events(begin, finish, ids))
        except CalendarError as e:
            problems.append(f"{source.name}: {e}")
    if not found and problems:
        raise CalendarError(" ".join(problems))
    return _listing(found, query, problems)


def event(event_id: str, occurrence: str = "") -> Event:
    source, native = _source_of(event_id)
    return source.event(native, _occurrence(occurrence))


def free_busy(start: str, end: str = "", calendar: str = "", hours: str = "09:00-18:00", min_minutes: int = 30) -> dict:
    """When the user is busy from `start` to `end` (merged events), the free times of at least `min_minutes`
    within `hours` each day, and the all-day events, which don't count as busy."""
    tz = times.user_zone()
    begin, finish = times.span(start, end, tz)
    first_hour, last_hour = _hours(hours)
    listing = events(start, end, calendar)
    busy = _busy([e for e in listing.events if not e.all_day and e.busy], begin, finish)
    shortest = timedelta(minutes=min_minutes)
    free = [
        gap
        for day in _days(begin, finish)
        for gap in _gaps(_within(day, first_hour, last_hour, begin, finish, tz), busy, shortest)
    ]
    result: dict = {
        "timezone": tz.key,
        "busy": [{"start": times.show(b.start, tz), "end": times.show(b.end, tz), "events": b.titles} for b in busy],
        "free": [{"start": times.show(s, tz), "end": times.show(f, tz)} for s, f in free],
        "all_day": [{"start": e.start, "end": e.end, "title": e.title} for e in listing.events if e.all_day],
    }
    if listing.problems:
        result["problems"] = listing.problems
    return result


# --- Writing ---


def create(
    title: str,
    start: str,
    end: str = "",
    all_day: bool = False,
    calendar: str = "",
    location: str = "",
    notes: str = "",
    timezone: str = "",
    repeat: str = "",
) -> Event:
    if not title.strip():
        raise CalendarError("An event needs a title.")
    tz = times.zone(timezone) if timezone.strip() else times.user_zone()
    new = NewEvent(title.strip(), times.timing(start, end, all_day, tz), location, notes, times.repeat(repeat))
    source, native = _source_of(_target(calendar).id)
    return source.create(native, new)


def update(
    event_id: str,
    occurrence: str = "",
    title: str | None = None,
    start: str | None = None,
    end: str | None = None,
    all_day: bool | None = None,
    location: str | None = None,
    notes: str | None = None,
    timezone: str | None = None,
) -> Event:
    """Change what's given. A new start without an end keeps the event's length."""
    if title is not None and not title.strip():
        raise CalendarError("An event needs a title.")
    source, native = _source_of(event_id)
    when = _occurrence(occurrence)
    timing = None
    if any(v is not None for v in (start, end, all_day, timezone)):
        timing = _new_timing(source.event(native, when), start, end, all_day, timezone)
    return source.update(native, when, Changes(title, location, notes, timing))


def delete(event_id: str, occurrence: str = "") -> None:
    source, native = _source_of(event_id)
    source.delete(native, _occurrence(occurrence))


# --- Sources and calendars ---


def _all() -> list[Source]:
    found = sources()
    if not found:
        raise CalendarError(_NOTHING_SET_UP)
    return found


def _source_of(any_id: str) -> tuple[Source, str]:
    key, native = _native(any_id)
    source = next((s for s in _all() if s.key == key), None)
    if source is None or not native:
        raise CalendarError(f"{any_id!r} isn't an id from these calendars: list_calendars and list_events give them.")
    return source, native


def _native(any_id: str) -> tuple[str, str]:
    key, _, native = any_id.strip().partition(":")
    return key, native


def _named(calendar: str) -> list[Calendar]:
    """The calendars with that id, or that name (several accounts can have a "Calendar")."""
    found, _ = calendars()
    wanted = calendar.strip()
    matches = [c for c in found if c.id == wanted] or [c for c in found if c.name.casefold() == wanted.casefold()]
    if not matches:
        names = ", ".join(sorted({c.name for c in found}))
        raise CalendarError(f"There's no calendar {wanted!r}. The calendars are: {names}.")
    return matches


def _target(calendar: str) -> Calendar:
    """The one calendar a new event goes to: the one named, else the default one."""
    if calendar.strip():
        matches = [c for c in _named(calendar) if c.writable]
        if len(matches) > 1:
            listed = ", ".join(f"{c.name} ({c.account}): {c.id}" for c in matches)
            raise CalendarError(f"Several calendars are called that; give the id of one: {listed}.")
        if not matches:
            raise CalendarError(f"The calendar {calendar.strip()!r} can't be changed: pick another one.")
        return matches[0]
    writable = [c for c in calendars()[0] if c.writable]
    target = next((c for c in writable if c.default), None) or next(iter(writable), None)
    if target is None:
        raise CalendarError("None of the calendars can be changed.")
    return target


def _listing(found: list[Event], query: str, problems: list[str]) -> Listing:
    needle = query.strip().casefold()
    if needle:
        found = [e for e in found if needle in f"{e.title}\n{e.location}\n{e.notes}".casefold()]
    # Times are all in the user's zone, so their text sorts in time order, a day's all-day events first.
    found.sort(key=lambda e: (e.start, e.title))
    return Listing(found[:MAX_EVENTS], problems, len(found) > MAX_EVENTS)


# --- Times ---


def _occurrence(text: str) -> date | datetime | None:
    return times.parse(text, times.user_zone()) if text.strip() else None


def _new_timing(
    current: Event, start: str | None, end: str | None, all_day: bool | None, timezone: str | None
) -> Timing:
    """When the event is after an update: what's given, the rest as it was, its length kept."""
    zone_name = timezone or current.timezone
    tz = times.zone(zone_name) if zone_name else times.user_zone()
    old = _timing_of(current, tz)
    first = times.parse(start, tz) if start is not None else old.start
    if all_day is None:
        all_day = not isinstance(first, datetime) if start is not None else isinstance(old, AllDay)
    if all_day:
        days = times.day_of(old.end) - times.day_of(old.start)
        return times.whole_days(first, times.parse(end, tz) if end is not None else times.day_of(first) + days)
    if not isinstance(first, datetime):
        raise CalendarError("Give the start as a time (2026-10-12T09:00) to make it a timed event.")
    length = old.end - old.start if isinstance(old, Timed) else timedelta(hours=1)
    return times.timed(first, times.parse(end, tz) if end is not None else first + length, tz)


def _timing_of(current: Event, tz: ZoneInfo) -> Timing:
    start, end = times.parse(current.start, tz), times.parse(current.end, tz)
    if isinstance(start, datetime) and isinstance(end, datetime):
        return Timed(start, end, tz)
    return AllDay(start, end)


def _hours(text: str) -> tuple[time, time]:
    try:
        first, last = (time.fromisoformat(part.strip()) for part in text.split("-"))
    except ValueError:
        raise CalendarError(f"{text!r} isn't a range of hours like 09:00-18:00.") from None
    if last <= first:
        raise CalendarError("The hours must end after they start, like 09:00-18:00.")
    return first, last


def _busy(timed: list[Event], begin: datetime, finish: datetime) -> list[_Busy]:
    """The times the events take up, overlapping ones merged, within the range."""
    merged: list[_Busy] = []
    for e in sorted(timed, key=lambda e: datetime.fromisoformat(e.start)):
        s, f = max(datetime.fromisoformat(e.start), begin), min(datetime.fromisoformat(e.end), finish)
        if merged and s <= merged[-1].end:
            merged[-1].end = max(merged[-1].end, f)
            merged[-1].titles.append(e.title)
        else:
            merged.append(_Busy(s, f, [e.title]))
    return merged


def _days(begin: datetime, finish: datetime) -> list[date]:
    """The days the range covers part of."""
    last = (finish - timedelta(microseconds=1)).date()
    return [begin.date() + timedelta(days=n) for n in range((last - begin.date()).days + 1)]


def _within(
    day: date, first_hour: time, last_hour: time, begin: datetime, finish: datetime, tz: ZoneInfo
) -> tuple[datetime, datetime]:
    """The day's hours, within the range."""
    return max(datetime.combine(day, first_hour, tz), begin), min(datetime.combine(day, last_hour, tz), finish)


def _gaps(hours: tuple[datetime, datetime], busy: list[_Busy], shortest: timedelta) -> list[tuple[datetime, datetime]]:
    """The free times within `hours` at least `shortest` long."""
    cursor, stop = hours
    gaps = []
    for b in busy:
        if b.end <= cursor or b.start >= stop:
            continue
        if b.start - cursor >= shortest:
            gaps.append((cursor, b.start))
        cursor = max(cursor, b.end)
    if stop - cursor >= shortest:
        gaps.append((cursor, stop))
    return gaps
