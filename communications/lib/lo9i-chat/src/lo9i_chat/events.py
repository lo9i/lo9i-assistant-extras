"""A run's events, as the daemon streams them: JSON objects with a "type" naming the event class and
its fields (the daemon's core/events.py and protocol.py)."""

from dataclasses import dataclass, fields
from typing import Any


@dataclass
class TextDelta:
    text: str


@dataclass
class ToolStarted:
    name: str
    args: dict[str, Any]


@dataclass
class ToolFinished:
    name: str
    output: str


@dataclass
class ApprovalRequired:
    """The run waits for the user. Usually an approval, answered with {"approved", "reason"}; a
    request whose `kind` is QUESTION is a question with options, with `question`, `options` and
    `multiple`, answered with {"choices": [picked options]}; one whose `kind` is SIDE_CONVERSATION
    suggests moving the task out of home, with `title`, `brief` and `first_message`, answered with
    {"moved": bool}."""

    interrupt_id: str
    request: dict[str, Any]


QUESTION = "question"


def is_question(request: dict[str, Any]) -> bool:
    return request.get("kind") == QUESTION


SIDE_CONVERSATION = "side_conversation"


def is_side_conversation(request: dict[str, Any]) -> bool:
    return request.get("kind") == SIDE_CONVERSATION


@dataclass
class Transcribed:
    """A voice message was transcribed; channels show it so the user can catch mistakes."""

    text: str
    language: str


@dataclass
class Done:
    pass


@dataclass
class Error:
    message: str


@dataclass
class Cancelled:
    """The user stopped the run before it finished."""


Event = TextDelta | ToolStarted | ToolFinished | ApprovalRequired | Transcribed | Done | Error | Cancelled

_TYPES: dict[str, type] = {
    cls.__name__: cls
    for cls in (TextDelta, ToolStarted, ToolFinished, ApprovalRequired, Transcribed, Done, Error, Cancelled)
}


class UnknownEventError(ValueError):
    """The daemon sent an event type this version doesn't know."""


def event_from_dict(data: dict[str, Any]) -> Event:
    """Fields this version doesn't know are left out, so a newer daemon adding one doesn't break it."""
    cls = _TYPES.get(str(data.get("type")))
    if cls is None:
        raise UnknownEventError(f"Unknown event type {data.get('type')!r}")
    known = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in data.items() if k in known})
