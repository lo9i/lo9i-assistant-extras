from pathlib import Path

import pytest
from yt_dlp.utils import DownloadError as YtDlpError

from lo9i_youtube import downloads


class FakeYoutubeDL:
    """Records the options and "downloads" by writing the file yt-dlp would."""

    seen: dict
    info: dict

    def __init__(self, opts):
        FakeYoutubeDL.seen = opts

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url, download, process):
        assert not download and not process
        if "missing" in url:
            raise YtDlpError("ERROR: [youtube] missing: Video unavailable")
        return FakeYoutubeDL.info

    def process_ie_result(self, info, download):
        path = Path(self.seen["outtmpl"].replace("%(title)s [%(id)s].%(ext)s", "Zoo [abc].mp3"))
        path.write_bytes(b"x" * 1_500_000)
        return info | {"requested_downloads": [{"filepath": str(path)}]}


@pytest.fixture(autouse=True)
def fake_ytdlp(monkeypatch, tmp_path):
    monkeypatch.setenv("YOUTUBE_DIR", str(tmp_path / "videos"))
    monkeypatch.setattr(downloads, "YoutubeDL", FakeYoutubeDL)
    FakeYoutubeDL.info = {"id": "abc", "title": "Zoo"}


def test_the_folder_is_the_field_or_downloads(monkeypatch, tmp_path):
    assert downloads.folder() == tmp_path / "videos"
    assert (tmp_path / "videos").is_dir()
    monkeypatch.setenv("YOUTUBE_DIR", "")
    monkeypatch.setenv("HOME", str(tmp_path))
    assert downloads.folder() == tmp_path / "Downloads"


def test_audio_is_an_mp3_with_tags_and_cover(tmp_path):
    done = downloads.audio("https://youtu.be/abc")
    assert done == downloads.Download("Zoo", str(tmp_path / "videos" / "Zoo [abc].mp3"), 1.5)
    keys = [p["key"] for p in FakeYoutubeDL.seen["postprocessors"]]
    assert keys == ["FFmpegExtractAudio", "FFmpegMetadata", "EmbedThumbnail"]
    assert FakeYoutubeDL.seen["noprogress"] and FakeYoutubeDL.seen["ffmpeg_location"]


def test_video_prefers_h264_up_to_the_height_asked():
    downloads.video("https://youtu.be/abc", max_height=720)
    assert FakeYoutubeDL.seen["format_sort"][:2] == ["res:720", "vcodec:h264"]
    assert FakeYoutubeDL.seen["merge_output_format"] == "mp4"


def test_a_playlist_is_refused_before_downloading_anything(tmp_path):
    FakeYoutubeDL.info = {"_type": "playlist", "title": "Mix"}
    with pytest.raises(downloads.DownloadError, match="is a playlist"):
        downloads.video("https://www.youtube.com/playlist?list=PL1")
    assert not any((tmp_path / "videos").iterdir())


def test_ytdlp_errors_lose_their_prefix():
    with pytest.raises(downloads.DownloadError) as e:
        downloads.audio("https://youtu.be/missing")
    assert str(e.value) == "[youtube] missing: Video unavailable"


def test_ffmpeg_falls_back_to_the_bundled_one(monkeypatch):
    monkeypatch.setattr(downloads.shutil, "which", lambda name: None)
    assert Path(downloads.ffmpeg()).name.startswith("ffmpeg")
