"""Approval messages with Approve / Reject buttons. Button data is `a:<interrupt id>` or
`r:<interrupt id>` for the chat's own conversation, `ja:` or `jr:` for a scheduled job's run (the
daemon's `approval` stream events)."""

from typing import Any

from lo9i_chat.port import Buttons

_APPROVE, _REJECT = "a", "r"
_JOB_APPROVE, _JOB_REJECT = "ja", "jr"


def approval_text(request: dict[str, Any]) -> str:
    return f"Approve {request.get('tool')}? ({request.get('reason')})\n\n{request.get('command', request)}"


def approval_buttons(interrupt_id: str) -> Buttons:
    return [("✅ Approve", f"{_APPROVE}:{interrupt_id}"), ("❌ Reject", f"{_REJECT}:{interrupt_id}")]


def parse_decision(data: str, place: str) -> tuple[str, dict[str, Any]] | None:
    """(interrupt id, decision) from button data, or None if it isn't an approval button. `place`
    names where a rejection happened, for example "Telegram"."""
    return _parse(data, _APPROVE, _REJECT, place)


def job_approval_text(title: str, request: dict[str, Any]) -> str:
    return f"{title} is waiting for you.\n\n{approval_text(request)}"


def job_approval_buttons(interrupt_id: str) -> Buttons:
    return [("✅ Approve", f"{_JOB_APPROVE}:{interrupt_id}"), ("❌ Reject", f"{_JOB_REJECT}:{interrupt_id}")]


def parse_job_decision(data: str, place: str) -> tuple[str, dict[str, Any]] | None:
    """Like `parse_decision`, for the buttons of a scheduled job's request."""
    return _parse(data, _JOB_APPROVE, _JOB_REJECT, place)


def _parse(data: str, approve: str, reject: str, place: str) -> tuple[str, dict[str, Any]] | None:
    action, _, interrupt_id = data.partition(":")
    if action not in (approve, reject) or not interrupt_id:
        return None
    approved = action == approve
    return interrupt_id, {"approved": approved, "reason": "" if approved else f"rejected in {place}"}
