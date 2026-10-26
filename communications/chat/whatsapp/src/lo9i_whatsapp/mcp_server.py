"""MCP server with the WhatsApp tools: find someone in the account's contacts, and write to them as the user.
lo9i starts it (server.yaml) with the plugin's data folder:

    WHATSAPP_DATA=/path/data lo9i-whatsapp-mcp

The WhatsApp connection is the channel's process; these tools ask it (tools.py). Finding is read-only. Sending
isn't, so lo9i asks the user before every message (the plugin isn't trusted) and denies it in scheduled runs.
"""

import os
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from lo9i_whatsapp.tools import ToolError, ask

mcp = MCPServer("whatsapp")

READ_ONLY = ToolAnnotations(read_only_hint=True)


@mcp.tool(annotations=READ_ONLY)
async def whatsapp_find_contact(name: str) -> str:
    """Find people in the user's WhatsApp contacts by name (every word must match), with their phone numbers,
    to write to them with whatsapp_send."""
    try:
        found = await ask(_folder(), {"op": "find", "query": name})
    except ToolError as e:
        return str(e)
    lines = [f"{f['name']}: {f['phone'] or '(number hidden by WhatsApp)'}" for f in found]
    return "\n".join(lines) or f"No WhatsApp contact matches {name!r}."


@mcp.tool()
async def whatsapp_send(to: str, text: str) -> str:
    """Send a WhatsApp message as the user to another person: `to` is their phone number with its country code,
    like +5491112345678 (from whatsapp_find_contact or the user's contacts). Write it as the user would, in their
    voice, without saying it's from an assistant unless they ask. The user approves every message. Their chat
    isn't read, so their answer reaches the user's phone, not you. Only people, not groups."""
    try:
        sent_to = await ask(_folder(), {"op": "send", "to": to, "text": text})
    except ToolError as e:
        return str(e)
    return f"Sent to {sent_to}."


def _folder() -> Path:
    return Path(os.environ["WHATSAPP_DATA"])


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
