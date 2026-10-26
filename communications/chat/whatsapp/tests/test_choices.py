"""lo9i's buttons as numbered options, and answers by number or name as the presses lo9i expects."""

from lo9i_whatsapp.choices import HINT, Choices, with_options

APPROVAL = [["✅ Approve", "a:i1"], ["❌ Reject", "r:i1"]]


def test_buttons_are_written_as_numbered_options():
    assert with_options("Approve bash?", APPROVAL) == f"Approve bash?\n\n*1.* ✅ Approve\n*2.* ❌ Reject\n{HINT}"
    assert with_options("Done.", None) == "Done."


def test_an_answer_by_number_or_name_presses_the_latest_offer_once():
    choices = Choices()
    choices.offer("M1", "Approve bash?", APPROVAL)
    pick = choices.pick("2")
    assert (pick.offer.message, pick.offer.text, pick.data) == ("M1", "Approve bash?", "r:i1")
    assert choices.pick("2") is None
    choices.offer("M2", "Approve git?", [["✅ Approve", "a:i2"], ["❌ Reject", "r:i2"]])
    assert choices.pick(" Approve ").data == "a:i2"


def test_a_quoted_offer_is_answered_even_when_a_later_one_waits():
    choices = Choices()
    choices.offer("M1", "Which day?", [["Monday", "qp:i1:0"], ["Tuesday", "qp:i1:1"]])
    choices.offer("M2", "Approve bash?", APPROVAL)
    assert choices.pick("tuesday", quoted="M1").data == "qp:i1:1"
    # Quoting a message without options is an ordinary reply.
    assert choices.pick("1", quoted="M9") is None


def test_ordinary_messages_and_answered_offers_are_not_presses():
    choices = Choices()
    choices.offer("M1", "Approve bash?", APPROVAL)
    assert choices.pick("approve it later, first tell me more") is None
    assert choices.pick("3") is None
    # An edit without buttons (lo9i shows the request was answered) ends the offer.
    choices.offer("M1", "Approve bash? ✅ Approved", None)
    assert choices.pick("1") is None
