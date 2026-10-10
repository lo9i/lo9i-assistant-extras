"""What the sources (caldav_source.py, mac_source.py) give and take: calendars and events, the same for every
source. Ids start with their source's key (`caldav:`, `mac:`), which routes a call back to it."""

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Protocol
from zoneinfo import ZoneInfo


class CalendarError(Exception):
    """A message for the agent: what went wrong, and what to do about it."""


@dataclass(frozen=True)
class Calendar:
    id: str
    name: str
    # Where it comes from: "iCloud", "Google" or "Mac" for the Calendar app's, "CalDAV (host)" for the account's.
    account: str
    writable: bool = True
    # Where new events go when no calendar is named.
    default: bool = False


@dataclass(frozen=True)
class Event:
    id: str
    calendar_id: str
    calendar: str
    title: str
    # In the user's time zone: 2026-10-12T09:00-03:00. An all-day event's are dates, its end the last day.
    start: str
    end: str
    all_day: bool
    location: str = ""
    notes: str = ""
    # Part of a repeating series. `occurrence` is when this one was scheduled, which picks it out of the
    # series for get_event, update_event and delete_event; "" for the series itself or a single event.
    recurring: bool = False
    occurrence: str = ""
    # The zone its times were set in, when it's not the user's; "" when they're the same or it has none.
    timezone: str = ""
    # It takes up the time (free_busy): not marked free, not cancelled.
    busy: bool = True
    attendees: list[str] = field(default_factory=list)
    url: str = ""


@dataclass(frozen=True)
class Timed:
    """When a timed event is: aware start and end, and the zone it's set in."""

    start: datetime
    end: datetime
    zone: ZoneInfo


@dataclass(frozen=True)
class AllDay:
    """When an all-day event is: its first and last days, the last included."""

    start: date
    end: date


Timing = Timed | AllDay


@dataclass(frozen=True)
class Repeat:
    """How an event repeats: the part of an iCalendar RRULE every source can save."""

    freq: str  # DAILY, WEEKLY, MONTHLY or YEARLY
    interval: int = 1
    count: int = 0
    until: date | None = None
    # Weekdays (MO, TU…) of a weekly event; none for the start's.
    days: tuple[str, ...] = ()


@dataclass(frozen=True)
class NewEvent:
    title: str
    timing: Timing
    location: str = ""
    notes: str = ""
    repeat: Repeat | None = None


@dataclass(frozen=True)
class Changes:
    """What update_event changes: None leaves it as it is, "" clears a text."""

    title: str | None = None
    location: str | None = None
    notes: str | None = None
    timing: Timing | None = None


class Source(Protocol):
    """Where calendars come from. Ids it takes are its own, without the `key:` prefix; ids it gives have it."""

    key: str
    name: str

    def calendars(self) -> list[Calendar]: ...

    def events(self, start: datetime, end: datetime, calendar_ids: list[str] | None) -> list[Event]: ...

    def event(self, event_id: str, occurrence: date | datetime | None) -> Event: ...

    def create(self, calendar_id: str, new: NewEvent) -> Event: ...

    def update(self, event_id: str, occurrence: date | datetime | None, changes: Changes) -> Event: ...

    def delete(self, event_id: str, occurrence: date | datetime | None) -> None: ...
