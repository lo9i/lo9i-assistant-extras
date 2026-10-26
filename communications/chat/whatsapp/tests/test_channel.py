"""The channel publishes what the apps show, carries out the setup buttons, and writes lo9i's chat operations in
the user's chat with themself, and nowhere else."""

import pytest

from lo9i_whatsapp.channel import WhatsAppChannel
from lo9i_whatsapp.choices import HINT, Choices
from lo9i_whatsapp.link import Link
from lo9i_whatsapp.port import SelfChat
from lo9i_whatsapp.runner import OperationError
from lo9i_whatsapp.status import LINK_CODE, NEW_QR, UNLINK

CHAT = "5491112345678@s.whatsapp.net"


class FakeClient:
    def __init__(self):
        self.statuses = []

    async def set_status(self, items):
        self.statuses.append([(i["kind"], i["label"], i["value"]) for i in items])


class FakeWhatsApp:
    """The account (sends and edits) and the setup's controls."""

    def __init__(self):
        self.sent, self.edits, self.pressed = [], [], []
        self.chat = CHAT

    async def send(self, text):
        self.sent.append(text)
        return f"W{len(self.sent)}"

    async def edit(self, message, text):
        self.edits.append((message, text))

    async def new_qr(self):
        self.pressed.append(NEW_QR)

    async def link_code(self):
        self.pressed.append(LINK_CODE)

    async def unlink(self):
        self.pressed.append(UNLINK)


@pytest.fixture
def setup(tmp_path):
    client, link, whatsapp, choices = FakeClient(), Link(tmp_path), FakeWhatsApp(), Choices()
    channel = WhatsAppChannel(client, link, whatsapp, SelfChat(whatsapp, choices), phone_given=True)
    return channel, client, link, whatsapp, choices


async def test_the_status_follows_the_link(setup):
    channel, client, link, _, _ = setup
    await channel.hello("Max", [])
    assert client.statuses[-1] == [("text", "", "Connecting to WhatsApp…")]
    await link.waiting("2@abc")
    assert client.statuses[-1][0][2].startswith("Not linked yet. Open the WhatsApp page")
    assert client.statuses[-1][-1] == ("action", "Link with a code instead", LINK_CODE)
    await link.code("ABCD-EFGH")
    assert ("code", "Link code", "ABCD-EFGH") in client.statuses[-1]
    await link.expired()
    assert client.statuses[-1] == [("text", "", "The QR code expired."), ("action", "New QR code", NEW_QR)]
    await link.linked("+5491112345678", "Ani")
    assert client.statuses[-1] == [
        ("text", "", "Linked to +5491112345678 (Ani)."),
        ("text", "", "Talk to the assistant in your WhatsApp chat with yourself (Message yourself)."),
        ("action", "Unlink", UNLINK),
    ]


async def test_the_setup_buttons_reach_the_connection(setup):
    channel, _, _, whatsapp, _ = setup
    for action in (NEW_QR, LINK_CODE, UNLINK, "something-else"):
        await channel.action(action)
    assert whatsapp.pressed == [NEW_QR, LINK_CODE, UNLINK]


async def test_messages_go_to_the_users_own_chat_marked_as_the_assistants(setup):
    channel, _, link, whatsapp, choices = setup
    await link.linked("+5491112345678", "Ani")
    assert await channel.operate("send", {"chat": "", "text": "**Done**", "markdown": True}) == "W1"
    buttons = [["✅ Approve", "a:i1"], ["❌ Reject", "r:i1"]]
    assert await channel.operate("send", {"chat": CHAT, "text": "Approve bash?", "buttons": buttons}) == "W2"
    assert whatsapp.sent == ["🤖 *Done*", f"🤖 Approve bash?\n\n*1.* ✅ Approve\n*2.* ❌ Reject\n{HINT}"]
    # The options wait for an answer until lo9i edits them away.
    await channel.operate("edit", {"chat": CHAT, "message": "W2", "text": "Approve bash?\n✅ Approved", "buttons": []})
    assert whatsapp.edits == [("W2", "🤖 Approve bash?\n✅ Approved")] and choices.pick("1") is None
    assert await channel.operate("typing", {"chat": CHAT}) == ""


async def test_nothing_is_written_anywhere_else_or_before_linking(setup):
    channel, _, link, whatsapp, _ = setup
    with pytest.raises(OperationError, match="isn't linked"):
        await channel.operate("send", {"chat": "", "text": "hi"})
    await link.linked("+5491112345678", "Ani")
    with pytest.raises(OperationError, match="only writes in the user's chat"):
        await channel.operate("send", {"chat": "5491187654321@s.whatsapp.net", "text": "hi"})
    with pytest.raises(OperationError, match="voice"):
        await channel.operate("voice", {"chat": "", "audio": "T2dnUw=="})
    assert whatsapp.sent == []
