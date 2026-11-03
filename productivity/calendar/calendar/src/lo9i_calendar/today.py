"""The day's events, for lo9i's list of the user's day (`today:` in server.yaml; lo9i's docs/today-lists.md): every
calendar's events of that day, in time order, each with its time (none for an all-day one) and where it is or its
calendar. Nothing needs doing about an event, so none says so. A calendar that couldn't be read is named in the note."""

from typing import Any

from lo9i_calendar import service
from lo9i_calendar.model import CalendarError, Event

_TEXTS = {
    "en": {"heading": "Calendar", "none": "No events today.", "problems": "Couldn't read: {problems}"},
    "es": {"heading": "Calendario", "none": "Sin eventos hoy.", "problems": "No se pudo leer: {problems}"},
}


def listed(day: str, language: str) -> dict[str, Any]:
    texts = _TEXTS.get(language, _TEXTS["en"])
    try:
        listing = service.events(day, "", "", "")
    except CalendarError as e:
        return {"heading": texts["heading"], "items": [], "note": texts["problems"].format(problems=e)}
    items = [_item(e, day) for e in listing.events]
    note = texts["problems"].format(problems="; ".join(listing.problems)) if listing.problems else ""
    return {"heading": texts["heading"], "items": items, "note": note or ("" if items else texts["none"])}


def _item(event: Event, day: str) -> dict[str, Any]:
    return {
        "title": event.title,
        "subtitle": event.location or event.calendar,
        "date": day,
        "time": "" if event.all_day else event.start[11:16],
        "url": event.url if event.url.startswith(("http://", "https://")) else "",
        "kind": "calendar",
        "key": f"calendar {event.id} {event.occurrence}".strip(),
    }
