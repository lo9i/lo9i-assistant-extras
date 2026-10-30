"""MCP server over stdio: look people up in the user's contacts, on a CardDAV server when the plugin was given
an account (carddav.py), else on Google Contacts when the user signed in with Google (google.py), else in the Mac's
Contacts (contacts.py). lo9i starts it (see server.yaml) with the account in CARDDAV_URL, CARDDAV_USER and
CARDDAV_PASSWORD, empty when none was given, and the Google sign-in in LO9I_OAUTH, empty until the user signs in."""

import asyncio
import os
from dataclasses import asdict
from functools import cache

import httpx2
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from lo9i_contacts import carddav, contacts, google
from lo9i_contacts.contacts import Contact

INSTRUCTIONS = """\
The people in the user's contacts. Look someone up with find_contact before asking the user for a number,
an email address, a birthday or an address. When several contacts match, ask which one before acting on it.
"""

mcp = MCPServer("contacts", instructions=INSTRUCTIONS)

READ_ONLY = ToolAnnotations(read_only_hint=True)


@cache
def _account() -> carddav.CardDav | None:
    """The CardDAV account the plugin was given, else Google Contacts once the user signed in with Google, or None
    to read the Mac's Contacts. Raises ContactsError for an account given only in part."""
    url, user, password = (os.environ.get(f"CARDDAV_{key}", "") for key in ("URL", "USER", "PASSWORD"))
    if carddav.configured(url, user, password):
        return carddav.CardDav(url.strip(), httpx2.BasicAuth(user.strip(), password))
    login = google.login_from_env()
    return google.contacts(login) if login else None


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


@mcp.tool(annotations=READ_ONLY)
async def check_account() -> str:
    """lo9i's check of the account the plugin was given, before it's saved (`check:` in server.yaml); the agent
    never gets it. Signs in to the CardDAV account and finds its address books; the Mac's Contacts aren't checked,
    since reading them asks the user through macOS."""
    try:
        account = _account()
        if account is not None:
            await account.check()
    except contacts.ContactsError as e:
        raise ToolError(str(e)) from None
    return "The account works."


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
