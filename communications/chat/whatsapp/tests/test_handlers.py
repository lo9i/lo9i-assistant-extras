"""What the user writes to themself goes to lo9i: commands with lo9i's answer, answers to options as presses,
messages with their file and the message they reply to."""

import pytest

from lo9i_whatsapp.choices import Choices
from lo9i_whatsapp.daemon import Attachment, RefusedError
from lo9i_whatsapp.handlers import DownloadError, Handlers
from lo9i_whatsapp.incoming import Incoming, Media

CHAT = "5491112345678@s.whatsapp.net"


class FakeDaemon:
    def __init__(self):
        self.messages, self.presses, self.commands = [], [], []

    async def message(self, chat, text, files=(), voice=False, reply_to="", message=""):
        self.messages.append((chat, text, list(files), voice, reply_to, message))

    async def press(self, chat, message, text, data):
        self.presses.append((chat, message, text, data))

    async def command(self, command, text=""):
        self.commands.append((command, text))
        return "Your next message starts a new conversation."


class FakeChat:
    def __init__(self):
        self.said = []

    async def say(self, text):
        self.said.append(text)
        return "S1"


async def _no_file() -> bytes:
    raise AssertionError("nothing to download")


@pytest.fixture
def parts():
    return FakeDaemon(), FakeChat(), Choices()


async def test_a_message_goes_to_lo9i_with_its_chat_id_and_reply(parts):
    daemon, chat, choices = parts
    await Handlers(daemon, chat, choices).message(CHAT, Incoming("M7", "and saturday?", "M5"), _no_file)
    assert daemon.messages == [(CHAT, "and saturday?", [], False, "M5", "M7")]


async def test_a_photo_and_a_voice_note_come_with_their_file(parts):
    daemon, chat, choices = parts

    async def photo() -> bytes:
        return b"\xff\xd8"

    image = Incoming("M8", "add this", media=Media("image", "image/jpeg", "photo-M8.jpg"))
    voice = Incoming("M9", "", media=Media("voice", "audio/ogg", "voice-M9.ogg"))
    await Handlers(daemon, chat, choices).message(CHAT, image, photo)
    await Handlers(daemon, chat, choices).message(CHAT, voice, photo)
    photo_file = Attachment("photo-M8.jpg", "image/jpeg", b"\xff\xd8")
    assert daemon.messages[0] == (CHAT, "add this", [photo_file], False, "", "M8")
    assert daemon.messages[1][3] is True


async def test_a_command_is_answered_with_lo9is_text(parts):
    daemon, chat, choices = parts
    handlers = Handlers(daemon, chat, choices)
    await handlers.message(CHAT, Incoming("M1", "/new"), _no_file)
    await handlers.message(CHAT, Incoming("M2", "/claude  ~/code/app "), _no_file)
    # A path isn't a command.
    await handlers.message(CHAT, Incoming("M3", "/Users/ani/notes.txt"), _no_file)
    assert daemon.commands == [("new", ""), ("claude", "~/code/app")]
    assert chat.said == ["Your next message starts a new conversation."] * 2
    assert [m[1] for m in daemon.messages] == ["/Users/ani/notes.txt"]


async def test_an_answer_to_options_is_a_press(parts):
    daemon, chat, choices = parts
    choices.offer("M4", "Approve bash?", [["✅ Approve", "a:i1"], ["❌ Reject", "r:i1"]])
    await Handlers(daemon, chat, choices).message(CHAT, Incoming("M5", "1"), _no_file)
    assert daemon.presses == [(CHAT, "M4", "Approve bash?", "a:i1")] and daemon.messages == []


async def test_what_cant_be_taken_is_said_in_the_chat(parts):
    daemon, chat, choices = parts

    async def refused(*args, **kwargs):
        raise RefusedError("The assistant is busy")

    async def broken() -> bytes:
        raise DownloadError("media expired")

    handlers = Handlers(daemon, chat, choices)
    await handlers.message(CHAT, Incoming("M1", "", unsupported="Videos aren't supported."), _no_file)
    await handlers.message(CHAT, Incoming("M2", "", media=Media("image", "image/jpeg", "p.jpg")), broken)
    daemon.message = refused
    await handlers.message(CHAT, Incoming("M3", "hi"), _no_file)
    assert chat.said == [
        "Videos aren't supported.",
        "Couldn't download the file from WhatsApp: media expired",
        "⚠️ The assistant is busy",
    ]
