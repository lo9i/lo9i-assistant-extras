"""Files the user sent with a message, as they're uploaded to the daemon."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Attachment:
    name: str
    media_type: str
    data: bytes
