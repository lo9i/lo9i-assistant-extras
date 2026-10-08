"""Turns Telegram photos, documents, voice notes and audio files into attachments."""

from telegram import Audio, Document, Message, PhotoSize, Voice

from lo9i_telegram.daemon import Attachment


class UnsupportedMediaError(Exception):
    pass


async def attachments_from(message: Message) -> list[Attachment]:
    if message.video_note:
        raise UnsupportedMediaError("Video messages aren't supported.")
    if message.voice:
        return [await _voice(message.voice)]
    if message.audio:
        return [await _audio(message.audio)]
    if message.photo:
        return [await _photo(message.photo[-1])]
    if message.document:
        return [await _document(message.document)]
    return []


async def _photo(largest: PhotoSize) -> Attachment:
    data = await (await largest.get_file()).download_as_bytearray()
    return Attachment(name=f"photo-{largest.file_unique_id}.jpg", media_type="image/jpeg", data=bytes(data))


async def _document(document: Document) -> Attachment:
    data = await (await document.get_file()).download_as_bytearray()
    name = document.file_name or f"file-{document.file_unique_id}"
    return Attachment(name=name, media_type=document.mime_type or "application/octet-stream", data=bytes(data))


async def _voice(voice: Voice) -> Attachment:
    data = await (await voice.get_file()).download_as_bytearray()
    return Attachment(
        name=f"voice-{voice.file_unique_id}.ogg", media_type=voice.mime_type or "audio/ogg", data=bytes(data)
    )


async def _audio(audio: Audio) -> Attachment:
    data = await (await audio.get_file()).download_as_bytearray()
    name = audio.file_name or f"audio-{audio.file_unique_id}"
    return Attachment(name=name, media_type=audio.mime_type or "audio/mpeg", data=bytes(data))
