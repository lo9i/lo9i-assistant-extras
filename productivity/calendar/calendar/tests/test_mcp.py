import json

from mcp import Client
from mcp.types import CallToolResult, TextContent

from lo9i_calendar import mcp_server, service
from lo9i_calendar.model import Calendar, CalendarError, Event

READ = {"list_calendars", "list_events", "get_event", "free_busy"}
WRITE = {"create_event", "update_event", "delete_event"}


def _text(result: CallToolResult) -> str:
    content = result.content[0]
    assert isinstance(content, TextContent)
    return content.text


async def _call(tool, /, **args):
    async with Client(mcp_server.mcp) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        result = await client.call_tool(tool, args)
    return tools, result


async def test_reading_is_read_only_and_changes_ask_first(monkeypatch):
    monkeypatch.setattr(service, "calendars", lambda: ([], []))
    tools, _ = await _call("list_calendars")
    assert set(tools) == READ | WRITE
    read_only = {name for name, tool in tools.items() if tool.annotations and tool.annotations.read_only_hint}
    assert read_only == READ


async def test_events_come_without_their_empty_fields(monkeypatch):
    event = Event("mac:1", "mac:work", "Work", "Dentist", "2026-10-12T15:00-03:00", "2026-10-12T16:00-03:00", False)
    monkeypatch.setattr(service, "events", lambda *args: service.Listing([event], ["Caldav: Wrong password."]))
    _, result = await _call("list_events", start="2026-10-12")
    assert json.loads(_text(result)) == {
        "timezone": "America/Argentina/Buenos_Aires",
        "events": [
            {
                "id": "mac:1",
                "calendar_id": "mac:work",
                "calendar": "Work",
                "title": "Dentist",
                "start": "2026-10-12T15:00-03:00",
                "end": "2026-10-12T16:00-03:00",
                "all_day": False,
                "busy": True,
            }
        ],
        "problems": ["Caldav: Wrong password."],
    }


async def test_calendars_and_problems(monkeypatch):
    monkeypatch.setattr(service, "calendars", lambda: ([Calendar("mac:work", "Work", "iCloud", default=True)], []))
    _, result = await _call("list_calendars")
    assert json.loads(_text(result)) == {
        "calendars": [{"id": "mac:work", "name": "Work", "account": "iCloud", "writable": True, "default": True}]
    }


async def test_calendar_errors_reach_the_agent(monkeypatch):
    def delete(event_id, occurrence):
        raise CalendarError("There's no event with that id: list_events gives the ids.")

    monkeypatch.setattr(service, "delete", delete)
    _, result = await _call("delete_event", id="mac:9")
    assert result.is_error and "no event with that id" in _text(result)


async def test_delete_says_what_went(monkeypatch):
    monkeypatch.setattr(service, "delete", lambda event_id, occurrence: None)
    _, result = await _call("delete_event", id="mac:1", occurrence="2026-10-19T09:00")
    assert _text(result) == "Deleted that occurrence."
