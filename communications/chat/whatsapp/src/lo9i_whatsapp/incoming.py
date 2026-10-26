"""Which WhatsApp messages are the user's to the assistant, and what they hold.

Only the user's chat with themself counts: a message the user wrote there (on the phone, or on another linked
device). Every other chat and group, and anything that isn't a message the user wrote (an edit, a reaction,
the assistant's own messages), is left alone here, before anything of it is read or logged."""

import time
from dataclasses import dataclass

from neonize.proto.Neonize_pb2 import JID
from neonize.proto.Neonize_pb2 import Message as MessageEvent
from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import Message

from lo9i_whatsapp.port import PREFIX

PHONE_SERVER = "s.whatsapp.net"
LID_SERVER = "lid"
# Messages older than this are dropped: notes the user wrote to themself while the assistant was off for long,
# which WhatsApp delivers when it connects again.
MAX_AGE_SECONDS = 15 * 60


@dataclass(frozen=True)
class Me:
    """The linked account: its phone number and its WhatsApp LID, the two ids its own chat goes by."""

    phone: str
    lid: str = ""

    @property
    def jid(self) -> JID:
        """The user's own chat, where the assistant writes."""
        return JID(User=self.phone, Server=PHONE_SERVER, Device=0, RawAgent=0, Integrator=0, IsEmpty=False)


@dataclass(frozen=True)
class Media:
    """A file in the message, to download: `kind` is image, document, audio or voice."""

    kind: str
    media_type: str
    name: str


@dataclass(frozen=True)
class Incoming:
    id: str
    text: str
    # The id of the message it quotes, when it's a reply.
    reply_to: str = ""
    media: Media | None = None
    # A kind of message the assistant can't take (a video), to tell the user.
    unsupported: str = ""


def is_own_chat(event: MessageEvent, me: Me) -> bool:
    """A message the user wrote in their chat with themself. Groups, broadcasts, channels and every chat with
    someone else are never it."""
    source = event.Info.MessageSource
    if source.IsGroup or not source.IsFromMe:
        return False
    return _is_me(source.Chat, me) and _is_me(source.Sender, me)


def parse(event: MessageEvent, me: Me, sent: set[str], now: float | None = None) -> Incoming | None:
    """The user's message to the assistant, or None to ignore it. `sent` holds the ids of the messages the
    assistant sent, which are never the user's."""
    if not is_own_chat(event, me) or event.Info.ID in sent or event.IsEdit:
        return None
    if (now if now is not None else time.time()) - _seconds(event.Info.Timestamp) > MAX_AGE_SECONDS:
        return None
    message = event.Message
    if message.HasField("protocolMessage") or message.HasField("reactionMessage"):
        return None
    incoming = _content(event.Info.ID, message)
    if incoming is None or incoming.text.startswith(PREFIX.strip()):
        return None
    return incoming


def _content(message_id: str, message: Message) -> Incoming | None:
    if message.conversation:
        return Incoming(message_id, message.conversation)
    if message.HasField("extendedTextMessage"):
        text = message.extendedTextMessage
        return Incoming(message_id, text.text, text.contextInfo.stanzaID)
    if message.HasField("imageMessage"):
        image = message.imageMessage
        media = Media("image", image.mimetype or "image/jpeg", f"photo-{message_id}{_extension(image.mimetype)}")
        return Incoming(message_id, image.caption, image.contextInfo.stanzaID, media)
    if message.HasField("documentMessage"):
        document = message.documentMessage
        media = Media("document", document.mimetype or "application/octet-stream", document.fileName or message_id)
        return Incoming(message_id, document.caption, document.contextInfo.stanzaID, media)
    if message.HasField("audioMessage"):
        audio = message.audioMessage
        kind = "voice" if audio.PTT else "audio"
        media = Media(kind, audio.mimetype or "audio/ogg", f"{kind}-{message_id}{_extension(audio.mimetype)}")
        return Incoming(message_id, "", audio.contextInfo.stanzaID, media)
    if message.HasField("videoMessage") or message.HasField("ptvMessage"):
        return Incoming(message_id, "", unsupported="Videos aren't supported.")
    return None


def _is_me(jid: JID, me: Me) -> bool:
    if jid.Server == PHONE_SERVER:
        return jid.User == me.phone
    if jid.Server == LID_SERVER:
        return bool(me.lid) and jid.User == me.lid
    return False


def _seconds(timestamp: int) -> float:
    """WhatsApp's timestamps come in seconds or, from some versions, in milliseconds."""
    return timestamp / 1000 if timestamp > 10**11 else float(timestamp)


_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "audio/ogg": ".ogg",
    "audio/mp4": ".m4a",
}


def _extension(media_type: str) -> str:
    return _EXTENSIONS.get(media_type.split(";")[0].strip(), "")
