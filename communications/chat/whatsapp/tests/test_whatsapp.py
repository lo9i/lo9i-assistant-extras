"""The WhatsApp connection, with a stand-in for neonize's client: QR codes until the account is linked, a new
connection after unlinking or when the QR codes ran out, the account never online, and only the user's own
messages, never the assistant's, handed on."""

import asyncio
import time
from types import SimpleNamespace

import pytest
from neonize.aioze.events import ConnectedEv, LoggedOutEv, MessageEv, TemporaryBanEv
from neonize.proto.Neonize_pb2 import JID, Device
from neonize.proto.Neonize_pb2 import Message as MessageEvent
from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import Message
from neonize.utils.enum import Presence

from lo9i_whatsapp import whatsapp as module
from lo9i_whatsapp.link import Link, State
from lo9i_whatsapp.pacing import Pacer
from lo9i_whatsapp.whatsapp import WhatsApp

PHONE = "5491112345678"
CHAT = f"{PHONE}@s.whatsapp.net"


def _jid(user: str, server: str = "s.whatsapp.net") -> JID:
    return JID(User=user, Server=server, Device=0, RawAgent=0, Integrator=0, IsEmpty=False)


class FakeClient:
    """What WhatsApp uses of neonize's NewAClient."""

    def __init__(self, db, fail: Exception | None = None):
        self.db, self.fail = db, fail
        self.handlers, self.on_qr = {}, None
        self.sent, self.edits, self.presences, self.logged_out = [], [], [], False
        self._stop = asyncio.Event()

    def qr(self, handler):
        self.on_qr = handler

    def event(self, kind):
        return lambda handler: self.handlers.__setitem__(kind, handler)

    async def connect(self):
        async def connection():
            if self.fail is not None:
                raise self.fail
            await self._stop.wait()

        return asyncio.create_task(connection())

    async def stop(self):
        self._stop.set()

    async def get_me(self):
        return Device(JID=_jid(PHONE), LID=_jid("1234567", "lid"), PushName="Ani", Platform="", BussinessName="")

    async def send_presence(self, presence):
        self.presences.append(presence)

    async def send_message(self, jid, message):
        self.sent.append((jid.User, message.conversation))
        return SimpleNamespace(ID=f"W{len(self.sent)}")

    async def edit_message(self, jid, message_id, message):
        self.edits.append((message_id, message.conversation))
        return SimpleNamespace(ID=f"E{len(self.edits)}")

    async def logout(self):
        self.logged_out = True

    async def PairPhone(self, phone, show_push_notification):  # noqa: N802 (neonize's name)
        return f"CODE-{phone[-4:]}"

    async def download_any(self, message):
        return b"\xff\xd8"


class Clients:
    """Makes the clients, keeping each; the ones in `failures` fail to connect, in order."""

    def __init__(self, *failures: Exception | None):
        self.made: list[FakeClient] = []
        self._failures = list(failures)

    def __call__(self, db):
        self.made.append(FakeClient(db, self._failures.pop(0) if self._failures else None))
        return self.made[-1]

    @property
    def last(self) -> FakeClient:
        return self.made[-1]


async def until(condition, seconds: float = 2.0) -> None:
    for _ in range(int(seconds / 0.01)):
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("it never happened")


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(module, "WRITE_GAP", 0)
    monkeypatch.setattr(module, "FIRST_RETRY", 0.05)
    monkeypatch.setattr(module, "_CHECK_SECONDS", 0.01)


@pytest.fixture
async def running(tmp_path):
    """A running connection, its link and its clients."""
    clients, link = Clients(), Link(tmp_path)
    whatsapp = WhatsApp(tmp_path, link, "+54 9 11 1234-5678", new_client=clients)
    task = asyncio.create_task(whatsapp.run())
    await until(lambda: clients.made)
    yield whatsapp, link, clients
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def _link(clients: Clients) -> None:
    await clients.last.handlers[ConnectedEv](clients.last, ConnectedEv(status=True))


def _from_me(text: str, message_id: str, chat: str = PHONE) -> MessageEvent:
    event = MessageEvent()
    event.Info.ID = message_id
    event.Info.Timestamp = int(time.time() * 1000)
    event.Info.MessageSource.Chat.CopyFrom(_jid(chat))
    event.Info.MessageSource.Sender.CopyFrom(_jid(PHONE))
    event.Info.MessageSource.IsFromMe = True
    event.Message.CopyFrom(Message(conversation=text))
    return event


async def test_a_qr_code_until_linked_then_the_account_stays_away(running):
    whatsapp, link, clients = running
    await clients.last.on_qr(clients.last, b"2@first,code")
    assert (link.now.state, link.now.qr) == (State.WAITING, "2@first,code")
    assert whatsapp.chat == ""
    await _link(clients)
    assert (link.now.state, link.now.phone, link.now.name) == (State.LINKED, f"+{PHONE}", "Ani")
    assert clients.last.presences == [Presence.UNAVAILABLE] and whatsapp.chat == CHAT


async def test_the_users_own_messages_are_handed_on_and_the_assistants_are_not(running):
    whatsapp, _, clients = running
    received = []

    async def on_message(chat, incoming, download):
        received.append((chat, incoming.id, incoming.text, await download()))

    whatsapp.on_message = on_message
    await _link(clients)
    assert await whatsapp.send("🤖 Hi") == "W1"
    await whatsapp.edit("W1", "🤖 Hi there")
    assert clients.last.sent == [(PHONE, "🤖 Hi")] and clients.last.edits == [("W1", "🤖 Hi there")]
    handle = clients.last.handlers[MessageEv]
    await handle(clients.last, _from_me("an echo without the mark", "W1"))
    await handle(clients.last, _from_me("to a friend", "F1", chat="5491187654321"))
    await handle(clients.last, _from_me("what's on today?", "U1"))
    assert received == [(CHAT, "U1", "what's on today?", b"\xff\xd8")]


async def test_unlinked_from_the_phone_shows_a_new_qr_code(running):
    _, link, clients = running
    await _link(clients)
    await clients.last.handlers[LoggedOutEv](clients.last, LoggedOutEv(OnConnect=False))
    await until(lambda: len(clients.made) == 2)
    assert link.now.state == State.STARTING and "unlinked" in link.now.problem
    await clients.last.on_qr(clients.last, b"2@again")
    assert link.now.state == State.WAITING


async def test_unlink_logs_out_and_starts_again(running):
    whatsapp, link, clients = running
    await _link(clients)
    first = clients.last
    await whatsapp.unlink()
    await until(lambda: len(clients.made) == 2)
    assert first.logged_out and link.now.state == State.STARTING and link.now.problem == ""


async def test_the_qr_codes_run_out_until_the_user_asks_for_another(running, monkeypatch, tmp_path):
    _, link, clients = running
    monkeypatch.setattr(module, "QR_LIFETIME", 0.05)
    await clients.last.on_qr(clients.last, b"2@soon,gone")
    await until(lambda: link.now.state == State.EXPIRED)
    await asyncio.sleep(0.05)
    assert len(clients.made) == 1
    # The page's button.
    (tmp_path / "new-qr").touch()
    await until(lambda: len(clients.made) == 2)
    monkeypatch.setattr(module, "QR_LIFETIME", 60)
    await clients.last.on_qr(clients.last, b"2@new")
    assert link.now.state == State.WAITING


async def test_a_code_to_type_for_the_number_entered(running):
    whatsapp, link, clients = running
    await clients.last.on_qr(clients.last, b"2@qr")
    await whatsapp.link_code()
    assert link.now.code == "CODE-5678"


async def test_unreachable_whatsapp_is_said_and_tried_again(tmp_path):
    clients, link = Clients(OSError("no network")), Link(tmp_path)
    task = asyncio.create_task(WhatsApp(tmp_path, link, new_client=clients).run())
    await until(lambda: link.now.state == State.FAILED)
    assert "no network" in link.now.problem
    await until(lambda: len(clients.made) == 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_a_ban_ends_the_connection_and_the_next_waits(running):
    _, link, clients = running
    await _link(clients)
    await clients.last.handlers[TemporaryBanEv](clients.last, TemporaryBanEv())
    assert link.now.state == State.FAILED and "banned" in link.now.problem
    await until(lambda: len(clients.made) == 2)


async def test_writes_are_kept_apart():
    now, slept = [100.0], []

    async def sleep(seconds):
        slept.append(seconds)
        now[0] += seconds

    pacer = Pacer(1.5, clock=lambda: now[0], sleep=sleep)
    await pacer.turn()
    now[0] += 0.5
    await pacer.turn()
    now[0] += 5
    await pacer.turn()
    assert slept == [1.0]
