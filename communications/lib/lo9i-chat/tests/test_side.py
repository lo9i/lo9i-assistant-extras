"""The suggestion to move a task out of home: its buttons, and the move once it's taken."""

from lo9i_chat.events import ApprovalRequired, Done, TextDelta
from lo9i_chat.moving import move_to_side
from lo9i_chat.side import parse_side
from lo9i_chat.turn import ChatTurn
from tests.fakes import LIMITS, FakeChat

_REQUEST = {"kind": "side_conversation", "title": "Taxes", "brief": "Do the 2026 return.", "first_message": "x"}


async def test_the_suggestion_shows_move_and_keep_buttons():
    async def events():
        yield ApprovalRequired("i1", _REQUEST)

    chat = FakeChat()
    await ChatTurn(chat, LIMITS).render(events())
    _, _, text, _, buttons = chat.log[-1]
    assert text == "Move to a side conversation? Taxes\n\nDo the 2026 return."
    assert buttons == [("➡️ Move", "sm:i1"), ("Keep it here", "sk:i1")]
    assert parse_side("sm:i1") == ("i1", True) and parse_side("sk:i1") == ("i1", False)
    assert parse_side("a:i1") is None and parse_side("sm:") is None


class _Daemon:
    def __init__(self):
        self.calls = []

    async def new_conversation(self):
        self.calls.append("new")
        return "t2"

    async def message(self, text, files=()):
        self.calls.append(text)
        yield TextDelta("On it.")
        yield Done()


async def test_moving_starts_a_side_conversation_with_the_brief():
    daemon, chat = _Daemon(), FakeChat()
    await move_to_side(daemon, chat, LIMITS, "Carried over from home: Taxes")
    assert daemon.calls == ["new", "Carried over from home: Taxes"]
    assert list(chat.messages.values()) == ["Moved to a side conversation. /home goes back.", "On it."]
