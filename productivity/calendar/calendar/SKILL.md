---
name: calendar
description: Reading and managing the user's calendars (the calendar MCP server) — their schedule, meetings, appointments and free time, in the Calendar app on their Mac and a CalDAV account. Use it for anything about what's on a day or week, when they're free, booking, moving or cancelling an event, or a date or time they mention for something to put on the calendar; and when they send a picture or text of an invitation, a ticket or a booking.
---

The `calendar` MCP server's tools are named `mcp__calendar__<tool>`: `list_calendars`, `list_events`, `get_event`, `free_busy`, `create_event`, `update_event` and `delete_event`. If they aren't there, the server is turned off: the user turns it on in Plugins.

## Times

- Everything is in the user's time zone (`list_events` says which). Write times as `2026-10-12T09:00`, without an offset, and dates as `2026-10-12`. Work out "tomorrow", "next Friday" or "this weekend" from today's date yourself.
- A date as `start` makes an all-day event. For an all-day event, `end` is its **last day**, included: a trip from the 14th to the 16th is `start: 2026-10-14, end: 2026-10-16`.
- Something in another time zone (a flight, a call with another country): give its times as written and that zone in `timezone` (`Europe/Madrid`). The event keeps that zone, and is listed in the user's.

## How to work

1. **What's on**: `list_events` with the day or range ("this week": Monday to Sunday). Without `start` it's today. Answer with times and titles, in order; mention all-day events first. Filter with `calendar` (a name) or `query` (a word in the title, place or notes) rather than reading everything.
2. **When am I free / find a time**: `free_busy` for the range, with `hours` when the user said a time of day ("in the morning": `08:00-12:00`) and `min_minutes` for the length needed. All-day events aren't counted as busy: say if one could matter (a holiday, a trip).
3. **Booking**: check the time with `list_events` or `free_busy` first and say if it clashes. Then `create_event` with a clear title, and the place and notes the user gave (an address, a link, a booking code). Leave `calendar` empty for the default one unless the user named one or it's obviously another (work calendar for a work meeting); `list_calendars` names them. `end` defaults to an hour later; ask only when the length really isn't clear.
4. **Repeating events**: `repeat` takes an RRULE: weekly on Mondays and Wednesdays until year end is `FREQ=WEEKLY;BYDAY=MO,WE;UNTIL=20261231`; ten sessions is `COUNT=10`; every two weeks is `INTERVAL=2`.
5. **Changing or cancelling**: find the event with `list_events` (or its `query`), then `update_event` or `delete_event` with its `id`. A repeating event's occurrences each have an `occurrence`: pass it to change or cancel only that one ("move tomorrow's standup"); leave it out for the whole series ("move the standup to 10 from now on"), and say which you're doing. A new `start` without `end` keeps the event's length. When several events fit, ask which one with `ask_user` before changing anything.
6. **From a picture or a message** (an invitation, a ticket, a confirmation email): read the title, date, time, place and zone from it, `create_event` with them, and put the booking reference or link in `notes`.

Adding, changing and deleting events ask the user for approval unless they trust the plugin; reading never does. After a change, confirm in one line: what, when (weekday and date), and the calendar if they have several.

## Problems

- An error names what to fix: an id that's gone (list the events again), a time written wrong, a calendar that can't be changed (a subscribed or birthdays one: pick another).
- `problems` in a result means an account couldn't be read while others were; answer with what came back and say which account failed and why.
- "No access to the Mac's calendars": the user allows it in System Settings → Privacy & Security → Calendars, with Full Access, for the app lo9i was started from. It only works while the assistant runs in their login session.
- A CalDAV account that refuses its password needs an app password: the user installs the plugin again from Plugins with the right one. Never ask for passwords in the chat.
