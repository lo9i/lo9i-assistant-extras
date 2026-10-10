"""Times as the agent writes them and as it reads them. Everything is in the user's time zone, which lo9i
gives its plugins as TZ, unless an event says otherwise."""

import os
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from lo9i_calendar.model import AllDay, CalendarError, Repeat, Timed, Timing

# The longest range one call reads.
MAX_DAYS = 366
_FREQS = ("DAILY", "WEEKLY", "MONTHLY", "YEARLY")
WEEKDAYS = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")


def user_zone() -> ZoneInfo:
    """The user's time zone: TZ, else UTC."""
    try:
        return ZoneInfo(os.environ.get("TZ", "").lstrip(":") or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name.strip())
    except (ZoneInfoNotFoundError, ValueError):
        raise CalendarError(f"{name!r} isn't a time zone: use an IANA name like Europe/Madrid.") from None


def parse(text: str, tz: ZoneInfo) -> date | datetime:
    """A date (2026-10-12), or a time (2026-10-12T09:00) in `tz` unless it has its own offset."""
    text = text.strip()
    try:
        if len(text) == 10:
            return date.fromisoformat(text)
        moment = datetime.fromisoformat(text)
    except ValueError:
        raise CalendarError(f"{text!r} isn't a date (2026-10-12) or a time (2026-10-12T09:00).") from None
    return moment.replace(tzinfo=tz) if moment.tzinfo is None else moment.astimezone(tz)


def midnight(day: date, tz: ZoneInfo) -> datetime:
    return datetime.combine(day, time(), tz)


def end_of_day(day: date, tz: ZoneInfo) -> datetime:
    """The last second of `day` in `tz`: where a timed event's repeat ends when it repeats until that day."""
    return midnight(day + timedelta(days=1), tz) - timedelta(seconds=1)


def show(value: date | datetime, tz: ZoneInfo) -> str:
    """For the agent: a date, or a time in `tz` to the minute (2026-10-12T09:00-03:00)."""
    if not isinstance(value, datetime):
        return value.isoformat()
    aware = value.replace(tzinfo=tz) if value.tzinfo is None else value
    return aware.astimezone(tz).isoformat(timespec="minutes")


def span(start: str, end: str, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """The range list_events reads: from `start` (today when empty) to `end`, a date meaning its whole day,
    and the end of the start's day when empty."""
    first = parse(start, tz) if start.strip() else datetime.now(tz).date()
    begin = first if isinstance(first, datetime) else midnight(first, tz)
    if end.strip():
        last = parse(end, tz)
        finish = last if isinstance(last, datetime) else midnight(last + timedelta(days=1), tz)
    else:
        finish = midnight(begin.date() + timedelta(days=1), tz)
    if finish <= begin:
        raise CalendarError("The end is before the start.")
    if finish - begin > timedelta(days=MAX_DAYS):
        raise CalendarError(f"Ask for at most {MAX_DAYS} days at a time.")
    return begin, finish


def timing(start: str, end: str, all_day: bool, tz: ZoneInfo) -> Timing:
    """When a new event is: all day when `all_day` or `start` is a date, its end its last day (the start's when
    empty); otherwise from `start` to `end`, an hour when empty."""
    first = parse(start, tz)
    if all_day or not isinstance(first, datetime):
        return whole_days(first, parse(end, tz) if end.strip() else first)
    last = parse(end, tz) if end.strip() else first + timedelta(hours=1)
    return timed(first, last, tz)


def whole_days(first: date, last: date) -> AllDay:
    """From `first`'s day to `last`'s, which may be times."""
    first_day, last_day = day_of(first), day_of(last)
    if last_day < first_day:
        raise CalendarError("The end is before the start.")
    return AllDay(first_day, last_day)


def timed(first: datetime, last: date, tz: ZoneInfo) -> Timed:
    if not isinstance(last, datetime):
        raise CalendarError("The end needs a time too (2026-10-12T10:00), or make it an all-day event.")
    if last <= first:
        raise CalendarError("The end must be after the start.")
    return Timed(first, last, tz)


def day_of(value: date) -> date:
    return value.date() if isinstance(value, datetime) else value


def repeat(rule: str) -> Repeat | None:
    """An RRULE like FREQ=WEEKLY;INTERVAL=2;BYDAY=MO,WE;UNTIL=20261231, of the parts every source can save."""
    rule = rule.strip().removeprefix("RRULE:")
    if not rule:
        return None
    try:
        parts = dict(part.split("=", 1) for part in rule.upper().split(";") if part)
    except ValueError:
        raise CalendarError(f"{rule!r} isn't an RRULE like FREQ=WEEKLY;BYDAY=MO.") from None
    unknown = set(parts) - {"FREQ", "INTERVAL", "COUNT", "UNTIL", "BYDAY"}
    if unknown or parts.get("FREQ") not in _FREQS:
        raise CalendarError(
            "repeat takes FREQ (DAILY, WEEKLY, MONTHLY or YEARLY), and optionally INTERVAL, COUNT or UNTIL "
            "(YYYYMMDD), and BYDAY (MO,TU…) for a weekly event."
        )
    days = tuple(d for d in parts.get("BYDAY", "").split(",") if d)
    if days and (parts["FREQ"] != "WEEKLY" or not set(days) <= set(WEEKDAYS)):
        raise CalendarError("BYDAY takes plain weekdays (MO,WE,FR), for a weekly event.")
    try:
        interval, count = int(parts.get("INTERVAL", 1)), int(parts.get("COUNT", 0))
        until = _until(parts["UNTIL"]) if "UNTIL" in parts else None
    except ValueError:
        raise CalendarError("INTERVAL and COUNT are numbers, UNTIL a date (20261231).") from None
    if interval < 1 or count < 0 or (count and until):
        raise CalendarError("INTERVAL is 1 or more, and a rule has COUNT or UNTIL, not both.")
    return Repeat(parts["FREQ"], interval, count, until, days)


def _until(text: str) -> date:
    return datetime.strptime(text[:8], "%Y%m%d").date()


def utc_key(value: date | datetime, tz: ZoneInfo) -> date | datetime:
    """To compare when occurrences were scheduled: a date, or the moment in UTC (a floating time is in `tz`)."""
    if not isinstance(value, datetime):
        return value
    aware = value.replace(tzinfo=tz) if value.tzinfo is None else value
    return aware.astimezone(UTC).replace(microsecond=0)
