"""Dialing through the Mac's Phone app: a `tel:` link opens it with the number, and macOS asks the user
to confirm before it calls, through their iPhone (Continuity's Calls from iPhone).

Opening it needs the user's login session: there's a screen there. When lo9i runs in it, the link is opened here
with `open`. When it started at boot, outside the session, lo9i gives the session helper's socket in
LO9I_LOGIN_SESSION, and the helper opens it in the session (lo9i's docs/login-session.md)."""

import asyncio
import json
import os
import re
import sys
from pathlib import Path

_PHONE_APP = Path("/System/Applications/Phone.app")
_OPEN = "/usr/bin/open"
SESSION_ENV = "LO9I_LOGIN_SESSION"
# Long enough for the Mac to open the Phone app.
_ANSWER_SECONDS = 15
# What people write between digits: spaces, dashes, dots, parentheses.
_SEPARATORS = re.compile(r"[\s\-.()]")
_NUMBER = re.compile(r"^\+?\d{3,15}$")


class DialError(Exception):
    pass


def normalize(number: str) -> str:
    """The number as a `tel:` link takes it: digits, with a leading + for an international one. Anything
    else is refused, so nothing but a number reaches the link."""
    compact = _SEPARATORS.sub("", number.strip())
    if not _NUMBER.match(compact):
        raise DialError(f"{number!r} isn't a phone number: use digits, with + and the country code if needed.")
    return compact


def available() -> bool:
    return sys.platform == "darwin" and _PHONE_APP.is_dir()


async def dial(number: str) -> str:
    """Opens the Phone app with the number. The call starts only when the user confirms on the Mac."""
    if not available():
        raise DialError("Calling needs a Mac with the Phone app.")
    target = normalize(number)
    session = os.environ.get(SESSION_ENV, "")
    await (_open_in_session(Path(session), f"tel:{target}") if session else _open_here(f"tel:{target}"))
    return target


async def _open_here(link: str) -> None:
    process = await asyncio.create_subprocess_exec(
        _OPEN, link, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    output, _ = await process.communicate()
    if process.returncode != 0:
        raise DialError(f"The Phone app didn't open: {output.decode(errors='replace').strip()}.")


async def _open_in_session(socket: Path, link: str) -> None:
    """Through lo9i's session helper, which answers while the user is logged in on the Mac."""
    try:
        reader, writer = await asyncio.open_unix_connection(str(socket))
    except OSError:
        raise DialError(_NOBODY_LOGGED_IN) from None
    try:
        writer.write(json.dumps({"op": "open", "url": link}).encode() + b"\n")
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), _ANSWER_SECONDS)
    except (OSError, TimeoutError):
        raise DialError(_NOBODY_LOGGED_IN) from None
    finally:
        writer.close()
    answer = json.loads(line) if line else {"error": "the helper didn't answer"}
    if "error" in answer:
        raise DialError(f"The Phone app didn't open: {answer['error']}.")


_NOBODY_LOGGED_IN = (
    "Nobody is logged in on the Mac: the Phone app opens on its screen, so log in on the Mac and try again."
)
