"""The agent's suggestion to move a focused task out of the home conversation (the daemon's
suggest_side_conversation tool) as Move / Keep buttons, `sm:<interrupt id>` or `sk:<interrupt id>`.
On Move, the run is answered, then the channel starts a side conversation and sends the brief there
(moving.py)."""

from typing import Any

from lo9i_chat.port import Buttons

_MOVE, _KEEP = "sm", "sk"


def side_text(request: dict[str, Any]) -> str:
    return f"Move to a side conversation? {request.get('title', '')}\n\n{request.get('brief', '')}"


def side_buttons(interrupt_id: str) -> Buttons:
    return [("➡️ Move", f"{_MOVE}:{interrupt_id}"), ("Keep it here", f"{_KEEP}:{interrupt_id}")]


def parse_side(data: str) -> tuple[str, bool] | None:
    """(interrupt id, moved) from button data, or None if it isn't a side conversation button."""
    action, _, interrupt_id = data.partition(":")
    if action not in (_MOVE, _KEEP) or not interrupt_id:
        return None
    return interrupt_id, action == _MOVE
