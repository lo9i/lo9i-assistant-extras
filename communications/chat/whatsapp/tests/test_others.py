"""Writing to someone else for the user: only numbers on WhatsApp, never the user's own, at most a few an hour;
finding them in the account's contacts. And how the tools' process reaches it, over the channel's socket."""

import asyncio
from types import SimpleNamespace

import pytest
from neonize.proto.Neonize_pb2 import JID, Contact, ContactInfo

from lo9i_whatsapp import mcp_server
from lo9i_whatsapp.incoming import Me
from lo9i_whatsapp.others import OTHERS_PER_HOUR, Others
from lo9i_whatsapp.tools import SOCKET, ToolError, ask, serve

ME = Me("5491112345678", "999")


def _jid(user: str, server: str = "s.whatsapp.net") -> JID:
    return JID(User=user, Server=server, Device=0, RawAgent=0, Integrator=0, IsEmpty=False)


def _contact(user: str, server: str = "s.whatsapp.net", **names: str) -> Contact:
    return Contact(JID=_jid(user, server), Info=ContactInfo(Found=True, **names))


class FakeClient:
    def __init__(self):
        self.contact = SimpleNamespace(get_all_contacts=self._contacts)

    async def is_on_whatsapp(self, number):
        return [SimpleNamespace(IsIn=number != "5491100000000", JID=_jid(number))]

    async def get_pn_from_lid(self, jid):
        return _jid("5491155554444")

    async def _contacts(self):
        return [
            _contact("5491187654321", FullName="Ana Gómez"),
            _contact("777", "lid", FirstName="Ana", PushName="Anita"),
            _contact("5491122223333", PushName="Juan"),
        ]


class FakeWhatsApp:
    def __init__(self, linked: bool = True):
        self.client, self.linked, self.written = FakeClient(), linked, []

    def account(self):
        if not self.linked:
            raise ConnectionError("WhatsApp isn't connected.")
        return self.client, ME

    async def write(self, jid, text):
        self.written.append((jid.User, text))
        return f"W{len(self.written)}"


async def test_a_message_goes_to_a_number_on_whatsapp():
    whatsapp = FakeWhatsApp()
    assert await Others(whatsapp).send("+54 9 11 8765-4321", "Llego en 10") == "+5491187654321"
    assert whatsapp.written == [("5491187654321", "Llego en 10")]


async def test_what_cant_be_sent_says_why():
    others = Others(FakeWhatsApp())
    for to, text, why in [
        ("Ana", "hi", "isn't a phone number"),
        ("+5491112345678", "hi", "user's own number"),
        ("+5491187654321", "  ", "empty"),
        ("+5491100000000", "hi", "isn't on WhatsApp"),
    ]:
        with pytest.raises(ToolError, match=why):
            await others.send(to, text)
    with pytest.raises(ToolError, match="isn't connected"):
        await Others(FakeWhatsApp(linked=False)).send("+5491187654321", "hi")


async def test_at_most_a_few_people_an_hour():
    now = [0.0]
    whatsapp = FakeWhatsApp()
    others = Others(whatsapp, clock=lambda: now[0])
    for _ in range(OTHERS_PER_HOUR):
        await others.send("+5491187654321", "hi")
    with pytest.raises(ToolError, match="in the last hour"):
        await others.send("+5491187654321", "one more")
    now[0] = 3601
    await others.send("+5491187654321", "later")
    assert len(whatsapp.written) == OTHERS_PER_HOUR + 1


async def test_contacts_are_found_by_name_with_their_numbers():
    found = await Others(FakeWhatsApp()).find("ana")
    assert [(f.name, f.phone) for f in found] == [("Ana", "+5491155554444"), ("Ana Gómez", "+5491187654321")]
    assert [f.name for f in await Others(FakeWhatsApp()).find("ana gó")] == ["Ana Gómez"]


async def test_the_tools_reach_the_channel_over_its_socket(tmp_path, monkeypatch):
    whatsapp = FakeWhatsApp()
    with pytest.raises(ToolError, match="isn't running"):
        await ask(tmp_path, {"op": "find", "query": "ana"})
    serving = asyncio.create_task(serve(tmp_path, Others(whatsapp)))
    while not (tmp_path / SOCKET).exists():
        await asyncio.sleep(0.01)
    assert (tmp_path / SOCKET).stat().st_mode & 0o777 == 0o600
    monkeypatch.setenv("WHATSAPP_DATA", str(tmp_path))
    assert await mcp_server.whatsapp_find_contact("juan") == "Juan: +5491122223333"
    assert await mcp_server.whatsapp_send("+5491187654321", "Llego en 10") == "Sent to +5491187654321."
    assert await mcp_server.whatsapp_send("+5491112345678", "hi") == (
        "That's the user's own number: write to them in this chat instead."
    )
    with pytest.raises(ToolError, match="can't read"):
        await ask(tmp_path, {"op": "delete-everything"})
    assert whatsapp.written == [("5491187654321", "Llego en 10")]
    serving.cancel()
