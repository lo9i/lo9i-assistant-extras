"""MCP server over stdio: look people up in the user's contacts, on a CardDAV server when the plugin was given
an account (carddav.py), else in the Mac's Contacts (contacts.py). lo9i starts it (see server.yaml) with the
account in CARDDAV_URL, CARDDAV_USER and CARDDAV_PASSWORD, empty when none was given."""

import asyncio
import os
from dataclasses import asdict
from functools import cache

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from lo9i_contacts import carddav, contacts
from lo9i_contacts.contacts import Contact

INSTRUCTIONS = """\
The people in the user's contacts. Look someone up with find_contact before asking the user for a number,
an email address, a birthday or an address. When several contacts match, ask which one before acting on it.
"""

mcp = MCPServer("contacts", instructions=INSTRUCTIONS)

READ_ONLY = ToolAnnotations(read_only_hint=True)


@cache
def _account() -> carddav.CardDav | None:
    """The CardDAV account the plugin was given, or None to read the Mac's Contacts. Raises ContactsError for
    one given only in part."""
    url, user, password = (os.environ.get(f"CARDDAV_{key}", "") for key in ("URL", "USER", "PASSWORD"))
    return carddav.CardDav(url.strip(), user.strip(), password) if carddav.configured(url, user, password) else None


async def _search(query: str) -> list[Contact]:
    account = _account()
    if account is not None:
        return await account.search(query)
    return await asyncio.to_thread(contacts.search, query)


@mcp.tool(annotations=READ_ONLY)
async def find_contact(query: str) -> list[dict]:
    """The user's contacts matching `query`: a name, an email address or a phone number. Each comes with its
    phone numbers, emails and addresses (labelled mobile, home, work…) and its birthday (YYYY-MM-DD, or --MM-DD
    without the year). Read from the CardDAV account the plugin was given, or else the Contacts on the user's
    Mac, where the first search asks the user, through macOS, to allow access."""
    try:
        found = await _search(query)
    except contacts.ContactsError as e:
        raise ToolError(str(e)) from None
    return [asdict(c) for c in found]


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
