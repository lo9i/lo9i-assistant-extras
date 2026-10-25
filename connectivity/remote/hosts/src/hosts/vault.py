"""The plugin's key, which encrypts each host's details in the database (db.py): lo9i makes one for the plugin
when it's installed (`data_key: true` in server.yaml), keeps it encrypted itself, and gives it to the plugin's
processes in LO9I_DATA_KEY (lo9i's core/mcp/data_key.py). It stays when the plugin is removed, so installing
it again opens the same hosts.
"""

import os

from cryptography.fernet import Fernet, InvalidToken

from .errors import LockedError

ENV = "LO9I_DATA_KEY"


def _fernet() -> Fernet:
    key = os.environ.get(ENV, "")
    if not key:
        raise LockedError(
            "lo9i didn't give the hosts plugin its key, so its hosts can't be opened. Update lo9i, then update the "
            "hosts plugin in Plugins."
        )
    try:
        return Fernet(key.encode())
    except ValueError:
        raise LockedError("The key lo9i gave the hosts plugin isn't one it can use. Update lo9i.") from None


def encrypt(text: str) -> str:
    return _fernet().encrypt(text.encode()).decode()


def decrypt(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken:
        raise LockedError(
            "The saved hosts were encrypted with another key than the one lo9i gives the plugin now, so they "
            "can't be read."
        ) from None
