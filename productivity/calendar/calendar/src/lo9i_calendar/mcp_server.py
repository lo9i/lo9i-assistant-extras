"""MCP server over stdio: the user's calendars, over CalDAV and on a Mac in the Calendar app. lo9i starts it
(see server.yaml) with TZ, the user's time zone, and the CalDAV account's CALDAV_URL, CALDAV_USERNAME and
CALDAV_PASSWORD when they were entered:

    TZ=Europe/Madrid CALDAV_URL=https://caldav.fastmail.com CALDAV_USERNAME=… CALDAV_PASSWORD=… calendar-mcp
"""

import asyncio
from collections.abc import Callable
from dataclasses import asdict
from typing import Annotated

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from lo9i_calendar import service, times
from lo9i_calendar.model import CalendarError, Event

INSTRUCTIONS = """\
The user's calendars: the ones in the Calendar app on their Mac (iCloud, Google, Exchange…) and a CalDAV
account. Times are in the user's time zone: give them as 2026-10-12T09:00, or a date (2026-10-12) for a
whole day. Before booking something, look at what's there with list_events or free_busy. Each occurrence
of a repeating event has an `occurrence`: pass it to change or delete only that one; without it, the
change applies to the whole series. Changes ask the user for approval unless they trust the plugin.
"""

mcp = MCPServer("calendar", instructions=INSTRUCTIONS)

# Lets clients run these without approval.
READ_ONLY = ToolAnnotations(read_only_hint=True)

Id = Annotated[str, Field(description="The event's id, from list_events")]
Occurrence = Annotated[
    str, Field(description="For a repeating event: the `occurrence` of the one meant; empty for the whole series")
]
Calendar = Annotated[str, Field(description="A calendar's name or id, from list_calendars; empty for all")]


async def _run[T](call: Callable[[], T]) -> T:
    """Calendar errors reach the model as tool errors it can act on."""
    try:
        return await asyncio.to_thread(call)
    except CalendarError as e:
        raise ToolError(str(e)) from None


def _event(e: Event) -> dict:
    """An event without its empty fields."""
    return {k: v for k, v in asdict(e).items() if v or k in ("title", "all_day", "busy")}


@mcp.tool(annotations=READ_ONLY)
async def list_calendars() -> dict:
    """The user's calendars, with their account, whether events can be added to them (writable), and the
    default one, where new events go when no calendar is named."""
    found, problems = await _run(service.calendars)
    result: dict = {"calendars": [asdict(c) for c in found]}
    if problems:
        result["problems"] = problems
    return result


@mcp.tool(annotations=READ_ONLY)
async def list_events(
    start: Annotated[str, Field(description="A date or a time; empty for today")] = "",
    end: Annotated[
        str, Field(description="A date (that day included) or a time; empty for the end of start's day")
    ] = "",
    calendar: Calendar = "",
    query: Annotated[str, Field(description="Only events with this in their title, location or notes")] = "",
) -> dict:
    """The events from `start` to `end`, in time order, at most a year at a time. Repeating events come as
    each occurrence. All-day events have dates, `end` being their last day. `problems` names calendars that
    couldn't be read."""
    listing = await _run(lambda: service.events(start, end, calendar, query))
    result: dict = {"timezone": times.user_zone().key, "events": [_event(e) for e in listing.events]}
    if listing.more:
        result["more"] = f"Only the first {service.MAX_EVENTS} events: ask for a shorter range."
    if listing.problems:
        result["problems"] = listing.problems
    return result


@mcp.tool(annotations=READ_ONLY)
async def get_event(id: Id, occurrence: Occurrence = "") -> dict:
    """One event with everything about it: its notes, attendees, link. A repeating event's series, or with
    `occurrence`, that one occurrence."""
    return _event(await _run(lambda: service.event(id, occurrence)))


@mcp.tool(annotations=READ_ONLY)
async def free_busy(
    start: Annotated[str, Field(description="A date or a time")],
    end: Annotated[
        str, Field(description="A date (that day included) or a time; empty for the end of start's day")
    ] = "",
    calendar: Calendar = "",
    hours: Annotated[str, Field(description="The hours of each day to find free time in")] = "09:00-18:00",
    min_minutes: Annotated[int, Field(description="The shortest free time worth listing", ge=1)] = 30,
) -> dict:
    """When the user is busy from `start` to `end` (overlapping events merged), and their free times within
    `hours` each day. Events marked free and cancelled ones don't count; all-day events (holidays, trips,
    birthdays) are listed apart, for you to judge."""
    return await _run(lambda: service.free_busy(start, end, calendar, hours, min_minutes))


@mcp.tool()
async def create_event(
    title: str,
    start: Annotated[str, Field(description="A time (2026-10-12T09:00), or a date for an all-day event")],
    end: Annotated[
        str, Field(description="A time; for an all-day event its last day. Empty: an hour later, or a single day")
    ] = "",
    all_day: Annotated[bool, Field(description="All day, on start's date (and up to end's)")] = False,
    calendar: Annotated[str, Field(description="A calendar's name or id; empty for the default one")] = "",
    location: str = "",
    notes: str = "",
    timezone: Annotated[
        str, Field(description="The IANA zone start and end are in, when not the user's (a flight's departure)")
    ] = "",
    repeat: Annotated[
        str,
        Field(description="To repeat: an RRULE of FREQ, INTERVAL, COUNT or UNTIL (YYYYMMDD), BYDAY (MO,WE) if weekly"),
    ] = "",
) -> dict:
    """Add an event to one of the user's calendars."""
    return _event(
        await _run(lambda: service.create(title, start, end, all_day, calendar, location, notes, timezone, repeat))
    )


@mcp.tool()
async def update_event(
    id: Id,
    occurrence: Occurrence = "",
    title: str | None = None,
    start: Annotated[
        str | None, Field(description="A time, or a date for all day. Without end, keeps the length")
    ] = None,
    end: Annotated[str | None, Field(description="A time; for an all-day event its last day")] = None,
    all_day: bool | None = None,
    location: Annotated[str | None, Field(description='"" removes it')] = None,
    notes: Annotated[str | None, Field(description='"" removes them')] = None,
    timezone: Annotated[
        str | None, Field(description="The IANA zone start and end are in, when not the user's")
    ] = None,
) -> dict:
    """Change an event: only what's given. For a repeating event, the occurrence given, or the whole series."""
    return _event(
        await _run(lambda: service.update(id, occurrence, title, start, end, all_day, location, notes, timezone))
    )


@mcp.tool()
async def delete_event(id: Id, occurrence: Occurrence = "") -> str:
    """Delete an event. For a repeating event, the occurrence given, or the whole series."""
    await _run(lambda: service.delete(id, occurrence))
    return "Deleted that occurrence." if occurrence.strip() else "Deleted."


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
