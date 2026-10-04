"""Reads a Server-Sent Events stream into (id, event kind, data) tuples."""

from collections.abc import AsyncIterator

import httpx


async def read_events(response: httpx.Response) -> AsyncIterator[tuple[str, str, str]]:
    """Comment lines (the daemon's keep-alive pings) have no field and are skipped."""
    event_id, kind, data = "", "message", []
    async for line in response.aiter_lines():
        if not line:
            if data:
                yield event_id, kind, "\n".join(data)
            kind, data = "message", []
            continue
        field, _, value = line.partition(":")
        value = value[1:] if value.startswith(" ") else value
        if field == "id":
            event_id = value
        elif field == "event":
            kind = value
        elif field == "data":
            data.append(value)
