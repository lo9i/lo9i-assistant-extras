"""Questions with options (the daemon's ask_user tool) as buttons. A single choice has one button per
option, `qp:<interrupt id>:<option>`, and a press answers. Several choices have a toggle per option,
`qt:<interrupt id>:<picked>:<option>`, and Done, `qd:<interrupt id>:<picked>`; `picked` is a bit mask
of the options picked so far, in hex, so the buttons carry the state and nothing is kept between
presses."""

from dataclasses import dataclass
from typing import Any

from lo9i_chat.port import Buttons

_PICK, _TOGGLE, _DONE = "qp", "qt", "qd"


@dataclass(frozen=True)
class Press:
    interrupt_id: str
    # Options picked after this press, as a bit mask.
    picked: int
    # True when the press answers; False when it only toggled an option.
    answers: bool


def question_text(request: dict[str, Any]) -> str:
    hint = "\n\nPick any, then Done." if request.get("multiple") else ""
    return f"{request.get('question', '')}{hint}"


def question_buttons(interrupt_id: str, request: dict[str, Any], picked: int = 0) -> Buttons:
    options = list(request.get("options", []))
    if not request.get("multiple"):
        return [(option, f"{_PICK}:{interrupt_id}:{i}") for i, option in enumerate(options)]
    toggles = [
        (f"{'☑' if picked >> i & 1 else '☐'} {option}", f"{_TOGGLE}:{interrupt_id}:{picked:x}:{i}")
        for i, option in enumerate(options)
    ]
    return [*toggles, ("✅ Done", f"{_DONE}:{interrupt_id}:{picked:x}")]


def parse_press(data: str) -> Press | None:
    """None if `data` isn't a question button."""
    action, _, rest = data.partition(":")
    parts = rest.split(":")
    try:
        if action == _PICK and len(parts) == 2:
            return Press(parts[0], 1 << int(parts[1]), answers=True)
        if action == _TOGGLE and len(parts) == 3:
            return Press(parts[0], int(parts[1], 16) ^ (1 << int(parts[2])), answers=False)
        if action == _DONE and len(parts) == 2:
            return Press(parts[0], int(parts[1], 16), answers=True)
    except ValueError:
        return None
    return None


def choices(request: dict[str, Any], picked: int) -> list[str]:
    return [option for i, option in enumerate(request.get("options", [])) if picked >> i & 1]


def answered_text(picked: list[str]) -> str:
    return f"Picked: {', '.join(picked)}." if picked else "Picked none."
