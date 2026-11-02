import asyncio
import json
import shutil
import tempfile
from pathlib import Path

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
    monkeypatch.delenv(dialer.SESSION_ENV, raising=False)
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
    with pytest.raises(dialer.DialError, match=r"didn't open: no session\."):
        await dialer.dial("1234567")


async def test_without_the_phone_app_nothing_is_dialed(monkeypatch):
    monkeypatch.setattr(dialer, "available", lambda: False)
    with pytest.raises(dialer.DialError, match="needs a Mac with the Phone app"):
        await dialer.dial("1234567")


@pytest.fixture
def helper(monkeypatch):
    """lo9i's session helper, as when lo9i runs from boot: it records what it's asked and answers ANSWER."""
    # Under /tmp: macOS limits socket paths to 104 characters, and pytest's tmp_path is longer.
    folder = Path(tempfile.mkdtemp(prefix="phone-"))
    socket = folder / "s.sock"
    asked: list[dict] = []
    answer = {"ok": True}

    async def serve(reader, writer):
        asked.append(json.loads(await reader.readline()))
        writer.write(json.dumps(answer).encode() + b"\n")
        await writer.drain()
        writer.close()

    monkeypatch.setattr(dialer, "available", lambda: True)
    monkeypatch.setenv(dialer.SESSION_ENV, str(socket))
    yield socket, serve, asked, answer
    shutil.rmtree(folder, ignore_errors=True)


async def test_from_boot_the_session_helper_opens_the_phone_app(helper, fake_open, monkeypatch):
    socket, serve, asked, _ = helper
    monkeypatch.setenv(dialer.SESSION_ENV, str(socket))
    async with await asyncio.start_unix_server(serve, path=str(socket)):
        assert await dialer.dial("+54 9 11 1234-5678") == "+5491112345678"
    assert asked == [{"op": "open", "url": "tel:+5491112345678"}]
    assert not fake_open.exists()  # Never opened outside the session.


async def test_the_helpers_refusal_is_the_error(helper):
    socket, serve, _, answer = helper
    answer.clear()
    answer["error"] = "No application knows how to open tel:1234567"
    async with await asyncio.start_unix_server(serve, path=str(socket)):
        with pytest.raises(dialer.DialError, match="didn't open: No application knows"):
            await dialer.dial("1234567")


async def test_with_nobody_logged_in_on_the_mac_it_says_so(helper):
    with pytest.raises(dialer.DialError, match="Nobody is logged in on the Mac"):
        await dialer.dial("1234567")
