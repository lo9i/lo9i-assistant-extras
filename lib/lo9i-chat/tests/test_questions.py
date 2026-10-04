"""Questions with options as chat buttons: the picks travel in the button data."""

from lo9i_chat.events import ApprovalRequired, Done
from lo9i_chat.questions import answered_text, choices, parse_press, question_buttons, question_text
from lo9i_chat.turn import ChatTurn
from tests.fakes import LIMITS, FakeChat

_ONE = {"kind": "question", "question": "Which one is you?", "options": ["Ana P.", "Ana R."]}
_SEVERAL = {**_ONE, "question": "Which sources?", "options": ["Google", "GitHub", "X"], "multiple": True}


def test_one_choice_answers_on_the_press():
    assert question_buttons("q1", _ONE) == [("Ana P.", "qp:q1:0"), ("Ana R.", "qp:q1:1")]
    press = parse_press("qp:q1:1")
    assert press is not None and press.answers and choices(_ONE, press.picked) == ["Ana R."]


def test_toggles_carry_the_picks_and_done_answers_with_them():
    first = parse_press(question_buttons("q1", _SEVERAL)[2][1])  # tick X
    assert first is not None and not first.answers
    buttons = question_buttons("q1", _SEVERAL, first.picked)
    assert [label for label, _ in buttons] == ["☐ Google", "☐ GitHub", "☑ X", "✅ Done"]
    second = parse_press(buttons[0][1])  # tick Google
    assert second is not None
    done = parse_press(question_buttons("q1", _SEVERAL, second.picked)[-1][1])
    assert done is not None and done.answers and choices(_SEVERAL, done.picked) == ["Google", "X"]
    assert answered_text(choices(_SEVERAL, done.picked)) == "Picked: Google, X."


def test_other_buttons_are_not_questions():
    assert parse_press("a:i1") is None and parse_press("qp:q1:x") is None and parse_press("qt:q1") is None


async def test_a_turn_shows_the_question_with_its_buttons():
    async def events():
        yield ApprovalRequired("q1", _SEVERAL)
        yield Done()

    chat = FakeChat()
    await ChatTurn(chat, LIMITS).render(events())
    [(_, _, text, _, buttons)] = [entry for entry in chat.log if entry[0] == "send"]
    assert text == question_text(_SEVERAL) and buttons == question_buttons("q1", _SEVERAL)
