"""Downloading a YouTube video, or its audio as MP3, with yt-dlp, into the download folder."""

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import imageio_ffmpeg
from yt_dlp import YoutubeDL
from yt_dlp.utils import YoutubeDLError


class DownloadError(Exception):
    """The download failed. The text says why."""


@dataclass
class Download:
    title: str
    path: str
    megabytes: float


def folder() -> Path:
    """YOUTUBE_DIR (the Download folder field), or ~/Downloads when it's empty."""
    configured = os.environ.get("YOUTUBE_DIR", "").strip()
    path = Path(configured).expanduser() if configured else Path.home() / "Downloads"
    path.mkdir(parents=True, exist_ok=True)
    return path


def ffmpeg() -> str:
    """The system's ffmpeg, or else the one imageio-ffmpeg brings, so nothing has to be installed."""
    return shutil.which("ffmpeg") or imageio_ffmpeg.get_ffmpeg_exe()


def video(url: str, max_height: int = 1080) -> Download:
    """The video as MP4, at the highest resolution up to `max_height`. H.264 when YouTube has it at that
    resolution, which every device plays; above 1080p it usually only has VP9 or AV1."""
    return _download(
        url,
        format="bv*+ba/b",
        format_sort=[f"res:{max_height}", "vcodec:h264", "acodec:aac"],
        merge_output_format="mp4",
    )


def audio(url: str) -> Download:
    """The audio as MP3, with the title, channel and thumbnail as its tags and cover."""
    return _download(
        url,
        format="bestaudio/best",
        writethumbnail=True,
        postprocessors=[
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "0"},
            {"key": "FFmpegMetadata"},
            {"key": "EmbedThumbnail"},
        ],
    )


def _download(url: str, **options: Any) -> Download:
    opts = {
        "outtmpl": str(folder() / "%(title)s [%(id)s].%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        # `quiet` doesn't cover the progress bar, which writes to stdout: the MCP connection.
        "noprogress": True,
        # YouTube hands out 403s and read timeouts often enough that a single try fails maybe half the
        # time. `extractor_retries` matters most: a 403 on the media URL needs fresh signed URLs, which
        # only re-extraction provides.
        "retries": 5,
        "fragment_retries": 5,
        "extractor_retries": 3,
        "socket_timeout": 30,
        "ffmpeg_location": ffmpeg(),
        **options,
    }
    try:
        with YoutubeDL(opts) as ydl:
            # Unprocessed, a playlist is only its URL's page, not every video in it.
            info = ydl.extract_info(url, download=False, process=False)
            # A playlist URL without a video in it would download every video in the playlist.
            if info.get("_type") == "playlist":
                raise DownloadError(f"{url} is a playlist, not a video: give the URL of one of its videos.")
            info = ydl.process_ie_result(info, download=True)
    except YoutubeDLError as e:
        raise DownloadError(str(e).removeprefix("ERROR: ")) from None
    path = Path(info["requested_downloads"][0]["filepath"])
    return Download(title=info.get("title", ""), path=str(path), megabytes=round(path.stat().st_size / 1e6, 1))
