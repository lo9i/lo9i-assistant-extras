"""The account's connection to WhatsApp, as a linked device of the user's own number, through neonize: whatsmeow
(the Go library for WhatsApp Web's multi-device protocol) built for each system and shipped in neonize's wheels,
so installing the plugin needs no Go, Node or compiler.

While the account isn't linked, WhatsApp gives QR codes for a few minutes (link.py shows them); when they run
out the connection ends and a new one starts when the user asks. Once linked, the session is kept in
`$WHATSAPP_DATA/whatsapp.db`, so restarts don't link again, and whatsmeow reconnects by itself when the
network drops. When the phone unlinks it (or the user does, with Unlink), whatsmeow deletes the session and a
new connection shows a new QR code.

Only the user's chat with themself is read (incoming.py) and written in; the account never says it's online
(which would stop the phone's notifications), never shows typing and never sends read receipts."""

import asyncio
import contextlib
import logging
import re
import time
from collections import deque
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from neonize.aioze.client import NewAClient
from neonize.aioze.events import (
    ClientOutdatedEv,
    ConnectedEv,
    LoggedOutEv,
    MessageEv,
    PairStatusEv,
    StreamReplacedEv,
    TemporaryBanEv,
)
from neonize.proto.waCompanionReg.WAWebProtobufsCompanionReg_pb2 import DeviceProps
from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import Message
from neonize.utils.enum import Presence

from lo9i_whatsapp.handlers import Download, DownloadError
from lo9i_whatsapp.incoming import Incoming, Me, parse
from lo9i_whatsapp.link import Link, State
from lo9i_whatsapp.pacing import Pacer

logger = logging.getLogger(__name__)

DB = "whatsapp.db"
# The phone lists the link as lo9i, in Linked devices. No full history: the channel reads none of it.
DEVICE = DeviceProps(os="lo9i", platformType=DeviceProps.DESKTOP, requireFullSync=False)
# The first QR code lasts 60 seconds and the next ones 20; none for this long means they ran out.
QR_LIFETIME = 75.0
# The least time between two messages or edits the account sends.
WRITE_GAP = 1.5
# How long to wait before connecting again after WhatsApp couldn't be reached, at first and at most.
FIRST_RETRY, MOST_RETRY = 5.0, 300.0
# The ids of the messages the assistant sent that are remembered, so they're never taken for the user's.
_SENT_KEPT = 2000
_CHECK_SECONDS = 2.0

OnMessage = Callable[[str, Incoming, Download], Awaitable[None]]
NewClient = Callable[[Path], Any]


def neonize_client(db: Path) -> NewAClient:
    # whatsmeow's own log is noisy and would reach lo9i's log; warnings and errors are enough.
    for name in ("neonize", "whatsmeow", "Whatsmeow"):
        logging.getLogger(name).setLevel(logging.WARNING)
    return NewAClient(str(db), props=DEVICE)


class WhatsApp:
    def __init__(self, folder: Path, link: Link, phone: str = "", new_client: NewClient = neonize_client) -> None:
        """`phone` is the number the user entered, for a code to type; "" when none."""
        self._db = folder / DB
        self._link = link
        self._phone = re.sub(r"\D", "", phone)
        self._new_client = new_client
        self._client: Any = None
        self._connection: asyncio.Task[Any] | None = None
        self._me: Me | None = None
        self._pacer = Pacer(WRITE_GAP)
        self._sent: deque[str] = deque(maxlen=_SENT_KEPT)
        # The connection should end: unlinked, or the QR codes ran out.
        self._ended = asyncio.Event()
        # WhatsApp refused the connection (a ban, an outdated version): the next one waits.
        self._refused = False
        self._new_qr = asyncio.Event()
        self._last_qr = 0.0
        # The user's messages are handled one at a time, in order.
        self._incoming = asyncio.Lock()
        self.on_message: OnMessage | None = None

    @property
    def chat(self) -> str:
        """The user's own chat's id, as lo9i knows it; "" while not linked."""
        return f"{self._me.phone}@s.whatsapp.net" if self._me is not None else ""

    async def run(self) -> None:
        """Keeps the account connected until cancelled."""
        self._db.parent.mkdir(parents=True, exist_ok=True)
        self._link.asked_for_new_qr()
        retry = FIRST_RETRY
        try:
            while True:
                if await self._connect():
                    retry = FIRST_RETRY
                else:
                    await self._pause(retry)
                    retry = min(retry * 2, MOST_RETRY)
                if self._link.now.state == State.EXPIRED:
                    await self._wait_for_new_qr()
        finally:
            await self._disconnect()

    # The chat's writes (port.py).

    async def send(self, text: str) -> str:
        client, me = self._linked()
        await self._pacer.turn()
        response = await client.send_message(me.jid, Message(conversation=text))
        self._sent.append(response.ID)
        return str(response.ID)

    async def edit(self, message: str, text: str) -> None:
        client, me = self._linked()
        await self._pacer.turn()
        response = await client.edit_message(me.jid, message, Message(conversation=text))
        self._sent.append(response.ID)

    # The setup's buttons (channel.py).

    async def new_qr(self) -> None:
        self._new_qr.set()

    async def link_code(self) -> None:
        """A code to type in WhatsApp instead of scanning, for the number the user entered."""
        if not self._phone or self._link.now.state != State.WAITING or self._client is None:
            return
        try:
            code = await self._client.PairPhone(self._phone, True)
        except Exception as e:
            logger.warning("WhatsApp gave no link code: %s", e)
            await self._link.note_problem(f"WhatsApp gave no link code: {e}")
            return
        await self._link.code(str(code))

    async def unlink(self) -> None:
        """Logs the account out (WhatsApp deletes the session) and starts again with a new QR code."""
        if self._client is None or self._me is None:
            return
        try:
            await self._client.logout()
        except Exception as e:
            logger.warning("Couldn't unlink: %s", e)
            await self._link.note_problem(f"Couldn't unlink: {e}")
            return
        self._me = None
        await self._link.unlinked("")
        self._ended.set()

    # One connection.

    async def _connect(self) -> bool:
        """Runs one connection until it ends. False when WhatsApp couldn't be reached."""
        self._ended.clear()
        self._refused = False
        if self._link.now.state not in (State.STARTING, State.LINKED):
            await self._link.starting()
        client = self._client = self._new_client(self._db)
        self._subscribe(client)
        self._last_qr = time.monotonic()
        self._connection = await client.connect()
        ended = asyncio.create_task(self._ended.wait())
        watch = asyncio.create_task(self._watch_qr())
        try:
            await asyncio.wait({self._connection, ended}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            ended.cancel()
            watch.cancel()
        if self._connection.done() and not self._connection.cancelled() and self._connection.exception():
            await self._unreachable(self._connection.exception())
            await self._disconnect()
            return False
        await self._disconnect()
        return not self._refused

    def _subscribe(self, client: Any) -> None:
        client.qr(self._guarded(self._on_qr))
        for event, handler in (
            (ConnectedEv, self._on_connected),
            (PairStatusEv, self._on_pair_status),
            (LoggedOutEv, self._on_logged_out),
            (TemporaryBanEv, self._on_banned),
            (ClientOutdatedEv, self._on_outdated),
            (StreamReplacedEv, self._on_replaced),
            (MessageEv, self._on_message),
        ):
            client.event(event)(self._guarded(handler))

    async def _disconnect(self) -> None:
        client, connection, self._client, self._connection = self._client, self._connection, None, None
        if client is None:
            return
        with contextlib.suppress(Exception):
            await client.stop()
        # A connection showing QR codes ignores the stop until they run out (whatsmeow waits for them), so it isn't
        # waited for long: its thread ends on its own, and the process ends without it (app.py).
        if connection is not None:
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await asyncio.wait_for(connection, 3)

    async def _unreachable(self, error: BaseException | None) -> None:
        logger.warning("Couldn't connect to WhatsApp: %s", error)
        await self._link.failed(f"Can't reach WhatsApp ({error}). Trying again.")

    async def _watch_qr(self) -> None:
        """WhatsApp stops giving QR codes after a few minutes; then the QR code expired."""
        while True:
            await asyncio.sleep(_CHECK_SECONDS)
            if self._link.now.state == State.WAITING and time.monotonic() - self._last_qr > QR_LIFETIME:
                await self._link.expired()
                self._ended.set()
                return

    async def _wait_for_new_qr(self) -> None:
        """Until the user asks for a new QR code, in the app's setup or on the page."""
        self._new_qr.clear()
        while not self._link.asked_for_new_qr():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._new_qr.wait(), _CHECK_SECONDS)
                break
        self._new_qr.clear()

    async def _pause(self, seconds: float) -> None:
        """A new QR code asked for cuts it short."""
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._new_qr.wait(), seconds)

    # WhatsApp's events.

    async def _on_qr(self, client: Any, code: bytes) -> None:
        self._last_qr = time.monotonic()
        await self._link.waiting(code.decode())

    async def _on_connected(self, client: Any, event: Any) -> None:
        device = await client.get_me()
        self._me = Me(device.JID.User, device.LID.User)
        # A linked device that says it's online stops the phone's notifications.
        try:
            await client.send_presence(Presence.UNAVAILABLE)
        except Exception as e:
            logger.warning("Couldn't tell WhatsApp the account is away: %s", e)
        await self._link.linked(f"+{self._me.phone}", device.PushName)

    async def _on_pair_status(self, client: Any, event: Any) -> None:
        if event.Error:
            await self._link.note_problem(f"Linking didn't work: {event.Error}")

    async def _on_logged_out(self, client: Any, event: Any) -> None:
        self._me = None
        await self._link.unlinked("WhatsApp unlinked this computer, from the phone or after a long time away.")
        self._ended.set()

    async def _on_banned(self, client: Any, event: Any) -> None:
        await self._link.failed(
            "WhatsApp has banned this account from linked devices for a while. The phone says until when."
        )
        self._refuse()

    async def _on_outdated(self, client: Any, event: Any) -> None:
        await self._link.failed("WhatsApp says this version is too old. Update the WhatsApp plugin in Plugins.")
        self._refuse()

    async def _on_replaced(self, client: Any, event: Any) -> None:
        await self._link.failed("Another program connected with this link. Turn it off, or unlink and link again.")
        self._refuse()

    def _refuse(self) -> None:
        """WhatsApp won't keep this connection: it ends, and the next one waits."""
        self._refused = True
        self._ended.set()

    async def _on_message(self, client: Any, event: Any) -> None:
        if self._me is None or self.on_message is None:
            return
        incoming = parse(event, self._me, set(self._sent))
        if incoming is None:
            return
        async with self._incoming:
            await self.on_message(self.chat, incoming, lambda: _download(client, event.Message))

    def _guarded(self, handler: Callable[..., Awaitable[None]]) -> Callable[..., Awaitable[None]]:
        """neonize runs handlers from its own thread, where what they raise is lost: it's logged here. An ended
        connection's events (a QR code it still shows) are ignored."""

        async def guarded(client: Any, event: Any) -> None:
            if client is not self._client:
                return
            try:
                await handler(client, event)
            except Exception:
                logger.exception("Handling a WhatsApp event failed")

        return guarded

    def _linked(self) -> tuple[Any, Me]:
        if self._client is None or self._me is None:
            raise ConnectionError("WhatsApp isn't connected.")
        return self._client, self._me


async def _download(client: Any, message: Message) -> bytes:
    try:
        return bytes(await client.download_any(message))
    except Exception as e:
        raise DownloadError(str(e)) from e
