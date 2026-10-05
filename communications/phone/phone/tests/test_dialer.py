import pytest

from lo9i_phone import dialer


@pytest.fixture
def fake_open(tmp_path, monkeypatch):
    """`open` that records its arguments, exiting with FAKE_EXIT."""
    record = tmp_path / "opened"
    script = tmp_path / "open"
    script.write_text(f'#!/bin/sh\necho "$@" > {record}\necho "no session" >&2\nexit "${{FAKE_EXIT:-0}}"\n')
    script.chmod(0o755)
    monkeypatch.setattr(dialer, "_OPEN", str(script))
    monkeypatch.setattr(dialer, "available", lambda: True)
    return record


@pytest.mark.parametrize(
    ("written", "dialed"),
    [("+54 9 11 1234-5678", "+5491112345678"), ("(011) 4555.1234", "01145551234"), ("911", "911")],
)
def test_numbers_are_written_as_a_tel_link_takes_them(written, dialed):
    assert dialer.normalize(written) == dialed


@pytest.mark.parametrize("written", ["call mom", "12", "+54 9 11;open -a Calculator", "tel:123456", "1+2345678"])
def test_anything_but_a_number_is_refused(written):
    with pytest.raises(dialer.DialError, match="isn't a phone number"):
        dialer.normalize(written)


async def test_dialing_opens_the_phone_app_with_the_number(fake_open):
    assert await dialer.dial("+54 9 11 1234-5678") == "+5491112345678"
    assert fake_open.read_text().strip() == "tel:+5491112345678"


async def test_when_the_phone_app_cant_open_the_error_says_why(fake_open, monkeypatch):
    monkeypatch.setenv("FAKE_EXIT", "1")
    with pytest.raises(dialer.DialError, match=r"didn't open: no session\. Calls work only while"):
        await dialer.dial("1234567")


async def test_without_the_phone_app_nothing_is_dialed(monkeypatch):
    monkeypatch.setattr(dialer, "available", lambda: False)
    with pytest.raises(dialer.DialError, match="needs a Mac with the Phone app"):
        await dialer.dial("1234567")
