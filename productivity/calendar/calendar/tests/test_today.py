"""The day's events for lo9i's list of the user's day: every calendar's, in time order, each with its time and place,
an all-day one without a time, the calendars that couldn't be read in the note, in the language asked."""

import json

from mcp import Client

from lo9i_calendar import mcp_server, service, today
from lo9i_calendar.model import CalendarError, Event

DENTIST = Event("mac:1", "mac:work", "Work", "Dentist", "2026-11-03T15:00-03:00", "2026-11-03T16:00-03:00", False)
HOLIDAY = Event("caldav:2", "caldav:home", "Home", "Holiday", "2026-11-03", "2026-11-03", True, location="Beach")


def test_the_days_events_with_their_time_and_place(monkeypatch):
    monkeypatch.setattr(service, "events", lambda *args: service.Listing([HOLIDAY, DENTIST]))
    listed = today.listed("2026-11-03", "en")
    assert listed["heading"] == "Calendar" and listed["note"] == ""
    assert [(i["title"], i["time"], i["subtitle"], i["date"]) for i in listed["items"]] == [
        ("Holiday", "", "Beach", "2026-11-03"),
        ("Dentist", "15:00", "Work", "2026-11-03"),
    ]
    assert listed["items"][1]["kind"] == "calendar" and listed["items"][1]["key"] == "calendar mac:1"


def test_no_events_and_calendars_that_couldnt_be_read(monkeypatch):
    monkeypatch.setattr(service, "events", lambda *args: service.Listing([], []))
    assert today.listed("2026-11-03", "es")["note"] == "Sin eventos hoy."
    monkeypatch.setattr(service, "events", lambda *args: service.Listing([DENTIST], ["Caldav: Wrong password."]))
    assert today.listed("2026-11-03", "en")["note"] == "Couldn't read: Caldav: Wrong password."

    def failing(*args):
        raise CalendarError("Calendar access was denied.")

    monkeypatch.setattr(service, "events", failing)
    assert today.listed("2026-11-03", "en") == {
        "heading": "Calendar",
        "items": [],
        "note": "Couldn't read: Calendar access was denied.",
    }


async def test_the_tool_answers_the_list_as_json(monkeypatch):
    monkeypatch.setattr(service, "events", lambda *args: service.Listing([DENTIST]))
    async with Client(mcp_server.mcp) as client:
        result = await client.call_tool("today", {"day": "2026-11-03", "language": "en"})
    assert not result.is_error and json.loads(result.content[0].text)["items"][0]["title"] == "Dentist"
