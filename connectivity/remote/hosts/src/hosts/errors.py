"""What keeps the plugin from reaching a host, in plain words for the user or the assistant."""


class UnknownHostError(Exception):
    """The list has no host by that name."""


class LockedError(Exception):
    """The hosts can't be read or saved: no key, or not the one they were saved with (vault.py)."""


class ConnectError(Exception):
    """Something the user has to fix before ssh can connect (connect.py)."""
