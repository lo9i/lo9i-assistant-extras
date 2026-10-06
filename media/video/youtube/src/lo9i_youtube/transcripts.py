"""A YouTube video's transcript (its captions) with youtube-transcript-api, as text."""

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
    language: str
    # YouTube's speech recognition made it, rather than whoever uploaded the video.
    generated: bool
    text: str


# youtube.com/watch?v=ID, youtu.be/ID, youtube.com/shorts/ID, /embed/ID, /live/ID, or the ID alone.
_ID = re.compile(r"(?:v=|youtu\.be/|/shorts/|/embed/|/live/|^)([\w-]{11})(?:[?&#/]|$)")


def video_id(url: str) -> str:
    """Raises TranscriptError when `url` isn't a YouTube video's URL or ID."""
    if match := _ID.search(url.strip()):
        return match.group(1)
    raise TranscriptError(f"{url} isn't the URL of a YouTube video.")


def fetch(url: str, languages: list[str] | None = None, timestamps: bool = False) -> Text:
    """The transcript in the first of `languages` (codes: en, es…) it's in, or in the language spoken in the
    video when none are given. One the uploader wrote is preferred to YouTube's generated one. With
    `timestamps`, one line per caption starting with [m:ss]; otherwise one paragraph."""
    vid = video_id(url)
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
    lines = [(c.start, " ".join(c.text.split())) for c in captions]
    if timestamps:
        text = "\n".join(f"[{_clock(start)}] {line}" for start, line in lines)
    else:
        text = " ".join(line for _, line in lines)
    return Text(language=transcript.language, generated=transcript.is_generated, text=text)


def save(url: str, languages: list[str] | None = None, timestamps: bool = False) -> tuple[Text, Path]:
    """The transcript, written to "<title> [<id>].txt" in the download folder."""
    found = fetch(url, languages, timestamps)
    vid = video_id(url)
    name = _safe(title(vid)) or vid
    path = downloads.folder() / f"{name} [{vid}].txt"
    path.write_text(found.text + "\n", encoding="utf-8")
    return found, path


def title(vid: str) -> str:
    """The video's title from YouTube's oEmbed, which answers quickly; empty if it doesn't."""
    try:
        response = requests.get(
            "https://www.youtube.com/oembed",
            params={"url": f"https://www.youtube.com/watch?v={vid}", "format": "json"},
            timeout=10,
        )
        response.raise_for_status()
        return str(response.json().get("title", ""))
    except (requests.RequestException, ValueError):
        return ""


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
