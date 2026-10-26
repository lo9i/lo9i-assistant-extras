"""Only what the user writes in their chat with themself reaches the assistant: every other chat, group and
broadcast, the assistant's own messages, edits, reactions and old notes are left alone."""

from neonize.proto.Neonize_pb2 import JID
from neonize.proto.Neonize_pb2 import Message as MessageEvent
from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import (
    AudioMessage,
    ContextInfo,
    ExtendedTextMessage,
    ImageMessage,
    Message,
    ReactionMessage,
    VideoMessage,
)

from lo9i_whatsapp.incoming import MAX_AGE_SECONDS, Incoming, Me, Media, parse

NOW = 1_800_000_000.0
ME = Me("5491112345678", "123456789012345")


def _jid(user: str, server: str = "s.whatsapp.net", device: int = 0) -> JID:
    return JID(User=user, Server=server, Device=device, RawAgent=0, Integrator=0, IsEmpty=False)


def _event(
    message: Message,
    chat: JID | None = None,
    sender: JID | None = None,
    from_me: bool = True,
    group: bool = False,
    message_id: str = "3EB0A1",
    seconds: float = NOW,
) -> MessageEvent:
    event = MessageEvent()
    event.Info.ID = message_id
    # WhatsApp's timestamps come in milliseconds.
    event.Info.Timestamp = int(seconds * 1000)
    source = event.Info.MessageSource
    source.Chat.CopyFrom(chat or _jid(ME.phone))
    source.Sender.CopyFrom(sender or _jid(ME.phone, device=3))
    source.IsFromMe, source.IsGroup = from_me, group
    event.Message.CopyFrom(message)
    return event


def _parse(event: MessageEvent, sent: set[str] = frozenset()) -> Incoming | None:
    return parse(event, ME, set(sent), now=NOW)


def test_a_message_to_myself_reaches_the_assistant():
    assert _parse(_event(Message(conversation="what's on today?"))) == Incoming("3EB0A1", "what's on today?")


def test_the_own_chat_also_goes_by_the_accounts_lid():
    lid = _jid(ME.lid, "lid")
    assert _parse(_event(Message(conversation="hi"), chat=lid, sender=lid)) == Incoming("3EB0A1", "hi")


def test_every_other_chat_and_group_is_ignored():
    text = Message(conversation="hi")
    friend = _jid("5491187654321")
    # What I write to a friend, what a friend writes to me, and a group, a broadcast or a channel.
    assert _parse(_event(text, chat=friend)) is None
    assert _parse(_event(text, chat=friend, sender=friend, from_me=False)) is None
    assert _parse(_event(text, chat=_jid("120363000000000000", "g.us"), group=True)) is None
    assert _parse(_event(text, chat=_jid("status", "broadcast"))) is None
    assert _parse(_event(text, chat=_jid("120363000000000001", "newsletter"))) is None
    # Someone else's message can't be in my own chat.
    assert _parse(_event(text, sender=friend, from_me=False)) is None


def test_the_assistants_own_messages_are_never_the_users():
    assert _parse(_event(Message(conversation="🤖 Done."), message_id="X1"), sent={"X1"}) is None
    # Without its id (the assistant restarted), the prefix still tells it.
    assert _parse(_event(Message(conversation="🤖 Done."))) is None


def test_edits_reactions_and_old_notes_are_ignored():
    edit = _event(Message(conversation="fixed"))
    edit.IsEdit = True
    assert _parse(edit) is None
    reaction = Message(reactionMessage=ReactionMessage(text="👍"))
    assert _parse(_event(reaction)) is None
    old = _event(Message(conversation="note to self"), seconds=NOW - MAX_AGE_SECONDS - 1)
    assert _parse(old) is None


def test_a_reply_names_the_message_it_quotes():
    reply = ExtendedTextMessage(text="and saturday?", contextInfo=ContextInfo(stanzaID="3EB0FF"))
    assert _parse(_event(Message(extendedTextMessage=reply))) == Incoming("3EB0A1", "and saturday?", "3EB0FF")


def test_photos_voice_notes_and_videos():
    photo = Message(imageMessage=ImageMessage(caption="add this", mimetype="image/jpeg"))
    image = Media("image", "image/jpeg", "photo-3EB0A1.jpg")
    assert _parse(_event(photo)) == Incoming("3EB0A1", "add this", media=image)
    voice = Message(audioMessage=AudioMessage(PTT=True, mimetype="audio/ogg; codecs=opus"))
    assert _parse(_event(voice)).media == Media("voice", "audio/ogg; codecs=opus", "voice-3EB0A1.ogg")
    video = Message(videoMessage=VideoMessage(caption="look"))
    assert _parse(_event(video)).unsupported == "Videos aren't supported."
