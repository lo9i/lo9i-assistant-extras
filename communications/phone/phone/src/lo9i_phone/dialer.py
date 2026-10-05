"""Dialing through the Mac's Phone app: a `tel:` link opens it with the number, and macOS asks the user
to confirm before it calls, through their iPhone (Continuity's Calls from iPhone)."""

import asyncio
import re
import sys
from pathlib import Path

_PHONE_APP = Path("/System/Applications/Phone.app")
_OPEN = "/usr/bin/open"
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
    process = await asyncio.create_subprocess_exec(
        _OPEN, f"tel:{target}", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    output, _ = await process.communicate()
    if process.returncode != 0:
        raise DialError(f"The Phone app didn't open: {output.decode(errors='replace').strip()}. {_NO_SESSION}")
    return target


# `open` fails when lo9i runs from boot (/startup), outside the user's login session: there's no screen to
# show the Phone app on.
_NO_SESSION = "Calls work only while lo9i runs in your login session, not when it was started at boot."
