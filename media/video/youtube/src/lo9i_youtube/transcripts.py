"""A YouTube video's transcript (its captions) with youtube-transcript-api, as text: in sections of a few
minutes, each starting with its time, so a model can summarize a long video section by section."""

import re
from dataclasses import dataclass
from pathlib import Path

import requests
from youtube_transcript_api import (
    CouldNotRetrieveTranscript,
    NoTranscriptFound,
    Transcript,
    TranscriptList,
    YouTubeTranscriptApi,
)

from lo9i_youtube import downloads


class TranscriptError(Exception):
    """There's no transcript to give. The text says why."""


@dataclass
class Text:
    title: str
    channel: str
    # m:ss or h:mm:ss, where the captions end: the video's length, give or take an ending without speech.
    duration: str
    language: str
    # YouTube's speech recognition made it, rather than whoever uploaded the video.
    generated: bool
    text: str
    # A long transcript comes in parts, one per call, so each fits in what the assistant reads at once.
    part: int = 1
    parts: int = 1


# A section starts at the first caption this many seconds after the one before it started.
SECTION_SECONDS = 300
# A part's most UTF-8 bytes: about 12k tokens of English, under 25k of Chinese or Japanese (Claude Code's
# default limit for what a tool returns).
PART_BYTES = 48_000
# Sound tags in captions: [Music], [Applause], [laughs].
_TAG = re.compile(r"\[[^\]]*\]")


# youtube.com/watch?v=ID, youtu.be/ID, youtube.com/shorts/ID, /embed/ID, /live/ID, or the ID alone.
_ID = re.compile(r"(?:v=|youtu\.be/|/shorts/|/embed/|/live/|^)([\w-]{11})(?:[?&#/]|$)")


def video_id(url: str) -> str:
    """Raises TranscriptError when `url` isn't a YouTube video's URL or ID."""
    if match := _ID.search(url.strip()):
        return match.group(1)
    raise TranscriptError(f"{url} isn't the URL of a YouTube video.")


def fetch(url: str, languages: list[str] | None = None, timestamps: bool = False, part: int = 1) -> Text:
    """The transcript in the first of `languages` (codes: en, es…) it's in, or in the language spoken in the
    video when none are given. One the uploader wrote is preferred to YouTube's generated one. It's in
    sections of about five minutes, each a paragraph starting with [m:ss]; with `timestamps`, one line per
    caption starting with [m:ss]. A long one is split in parts at sections (or lines): this is `part`."""
    vid = video_id(url)
    transcript, lines = _captions(vid, languages)
    if timestamps:
        units = [f"[{_clock(start)}] {line}" for start, line, _ in lines]
    else:
        units = [f"[{_clock(start)}] {text}" for start, text in _sections(lines)]
    parts = _parts(units, "\n" if timestamps else "\n\n")
    if not 1 <= part <= len(parts):
        raise TranscriptError(f"There's no part {part}: it has {len(parts)}.")
    title, channel = about(vid)
    return Text(
        title=title,
        channel=channel,
        duration=_clock(lines[-1][2] if lines else 0),
        language=transcript.language,
        generated=transcript.is_generated,
        text=parts[part - 1],
        part=part,
        parts=len(parts),
    )


def save(url: str, languages: list[str] | None = None, timestamps: bool = False) -> tuple[Transcript, Path]:
    """The whole transcript, written to "<title> [<id>].txt" in the download folder: a paragraph per section,
    or with `timestamps` a line per caption starting with [m:ss]."""
    vid = video_id(url)
    transcript, lines = _captions(vid, languages)
    if timestamps:
        text = "\n".join(f"[{_clock(start)}] {line}" for start, line, _ in lines)
    else:
        text = "\n\n".join(section for _, section in _sections(lines))
    name = _safe(about(vid)[0]) or vid
    path = downloads.folder() / f"{name} [{vid}].txt"
    path.write_text(text + "\n", encoding="utf-8")
    return transcript, path


def about(vid: str) -> tuple[str, str]:
    """The video's title and channel from YouTube's oEmbed, which answers quickly; empty if it doesn't."""
    try:
        response = requests.get(
            "https://www.youtube.com/oembed",
            params={"url": f"https://www.youtube.com/watch?v={vid}", "format": "json"},
            timeout=10,
        )
        response.raise_for_status()
        found = response.json()
        return str(found.get("title", "")), str(found.get("author_name", ""))
    except (requests.RequestException, ValueError, AttributeError):
        return "", ""


def _captions(vid: str, languages: list[str] | None) -> tuple[Transcript, list[tuple[float, str, float]]]:
    """The transcript picked (see fetch) and its captions as (start, text, end), cleaned (see _clean)."""
    try:
        found = YouTubeTranscriptApi().list(vid)
        transcript = _pick(found, languages)
        captions = transcript.fetch()
    except NoTranscriptFound:
        wanted = ", ".join(languages or [])
        raise TranscriptError(f"It has no transcript in {wanted}; it has: {_available(found)}.") from None
    except CouldNotRetrieveTranscript as e:
        # The library's message explains the cause and then how to file a bug; only the cause is for the user.
        raise TranscriptError(str(e).split("\n\nIf you are sure")[0].strip()) from None
    return transcript, _clean(captions)


def _clean(captions) -> list[tuple[float, str, float]]:
    """Each caption as (start, text, end), its whitespace collapsed, without sound tags ([Music]), and without
    what repeats the caption before it: a generated one at times says a line again, or starts with the last
    words of the one before. Captions left empty are dropped."""
    lines: list[tuple[float, str, float]] = []
    last: list[str] = []
    for caption in captions:
        words = _TAG.sub(" ", caption.text).split()
        # The longest start of this caption the previous one ends with: all of it, or three words or more.
        repeats = (n for n in range(min(len(words), len(last)), 0, -1) if words[:n] == last[-n:])
        repeated = next((n for n in repeats if n >= 3 or n == len(words)), 0)
        if words[repeated:]:
            lines.append((caption.start, " ".join(words[repeated:]), caption.start + caption.duration))
            last = words
    return lines


def _sections(lines: list[tuple[float, str, float]]) -> list[tuple[float, str]]:
    """The captions joined in sections of about SECTION_SECONDS, as (start, text)."""
    sections: list[tuple[float, list[str]]] = []
    for start, line, _ in lines:
        if not sections or start - sections[-1][0] >= SECTION_SECONDS:
            sections.append((start, []))
        sections[-1][1].append(line)
    return [(start, " ".join(text)) for start, text in sections]


def _parts(units: list[str], separator: str) -> list[str]:
    """The sections (or lines) joined by `separator` in parts of up to PART_BYTES; a part has one at least."""
    parts: list[list[str]] = [[]]
    size = 0
    for unit in units:
        grown = len(unit.encode()) + len(separator)
        if parts[-1] and size + grown > PART_BYTES:
            parts.append([])
            size = 0
        parts[-1].append(unit)
        size += grown
    return [separator.join(part) for part in parts]


def _pick(found: TranscriptList, languages: list[str] | None) -> Transcript:
    if languages:
        return found.find_transcript(languages)
    # YouTube only generates transcripts in the language spoken, so a generated one names it. A written one
    # in another language is usually a translation.
    spoken = next((t.language_code for t in found if t.is_generated), None)
    if spoken:
        return found.find_transcript([spoken])
    if written := next(iter(found), None):
        return written
    raise TranscriptError("It has no transcript.")


def _available(found: TranscriptList) -> str:
    return ", ".join(f"{t.language} ({t.language_code})" for t in found) or "none"


def _clock(seconds: float) -> str:
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02}:{secs:02}" if hours else f"{minutes}:{secs:02}"


def _safe(name: str) -> str:
    """`name` without the characters file systems refuse."""
    return re.sub(r'[\\/:*?"<>|\x00-\x1f]', "", name).strip(" .")
