"""What the apps show for a channel (`PUT /channels/<name>/status`): a list of setup items."""

from dataclasses import asdict, dataclass
from typing import Any, Literal

Kind = Literal["text", "code", "copy", "action"]


@dataclass(frozen=True)
class SetupItem:
    """`text` is a line, `code` a short value to read out, `copy` a longer value to copy, and
    `action` a button whose `value` is the action id the daemon sends back in an `action` event."""

    kind: Kind
    label: str
    value: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def text(line: str) -> SetupItem:
    return SetupItem("text", "", line)


def code(label: str, value: str) -> SetupItem:
    return SetupItem("code", label, value)


def copy(label: str, value: str) -> SetupItem:
    return SetupItem("copy", label, value)


def action(label: str, action_id: str) -> SetupItem:
    return SetupItem("action", label, action_id)
