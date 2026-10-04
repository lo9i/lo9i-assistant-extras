"""Approval buttons, splitting and event decoding."""

import pytest

from lo9i_chat.approvals import job_approval_buttons, parse_decision, parse_job_decision
from lo9i_chat.events import ApprovalRequired, Done, TextDelta, UnknownEventError, event_from_dict
from lo9i_chat.split import split_text


def test_parse_decision():
    assert parse_decision("a:abc", "Telegram") == ("abc", {"approved": True, "reason": ""})
    rejected = parse_decision("r:abc", "Telegram")
    assert rejected is not None and rejected[1]["approved"] is False
    assert parse_decision("junk", "Telegram") is None


def test_job_buttons_and_chat_buttons_are_told_apart():
    assert [data for _, data in job_approval_buttons("i1")] == ["ja:i1", "jr:i1"]
    assert parse_job_decision("ja:i1", "Telegram") == ("i1", {"approved": True, "reason": ""})
    assert parse_job_decision("jr:i1", "Telegram") == ("i1", {"approved": False, "reason": "rejected in Telegram"})
    assert parse_job_decision("a:i1", "Telegram") is None and parse_decision("ja:i1", "Telegram") is None


def test_split_text_respects_the_limit():
    parts = split_text("para one\n\n" + "x" * 5000 + "\n\nend", limit=3500)
    assert all(len(p) <= 3500 for p in parts) and "".join(parts).replace("\n", "").endswith("end")


def test_events_decode_from_the_daemons_json():
    assert event_from_dict({"type": "TextDelta", "text": "hi"}) == TextDelta("hi")
    assert event_from_dict({"type": "Done"}) == Done()
    approval = event_from_dict({"type": "ApprovalRequired", "interrupt_id": "i", "request": {}, "new": 1})
    assert approval == ApprovalRequired("i", {})
    with pytest.raises(UnknownEventError):
        event_from_dict({"type": "Nope"})
