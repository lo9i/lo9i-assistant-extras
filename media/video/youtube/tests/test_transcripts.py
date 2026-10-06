from dataclasses import dataclass, field

import pytest
from youtube_transcript_api import NoTranscriptFound, TranscriptsDisabled

from lo9i_youtube import transcripts


@dataclass
class Caption:
    text: str
    start: float


@dataclass
class FakeTranscript:
    language: str
    language_code: str
    is_generated: bool
    captions: list[Caption] = field(default_factory=lambda: [Caption("Hello\nthere", 1.2), Caption("again", 65.0)])

    def fetch(self):
        return self.captions


class FakeList:
    def __init__(self, *found: FakeTranscript):
        self.found = found

    def __iter__(self):
        # As the library's: the ones the uploader wrote first.
        return iter(sorted(self.found, key=lambda t: t.is_generated))

    def find_transcript(self, codes):
        for code in codes:
            for t in self:
                if t.language_code == code:
                    return t
        raise NoTranscriptFound("abcdefghijk", codes, self)


@pytest.fixture
def listing(monkeypatch):
    """Sets what YouTube has for the video."""

    def use(*found):
        class Api:
            def list(self, vid):
                if not found:
                    raise TranscriptsDisabled(vid)
                return FakeList(*found)

        monkeypatch.setattr(transcripts, "YouTubeTranscriptApi", Api)

    return use


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/watch?v=jNQXAC9IVRw",
        "https://www.youtube.com/watch?feature=share&v=jNQXAC9IVRw&t=10",
        "https://youtu.be/jNQXAC9IVRw?si=xyz",
        "https://www.youtube.com/shorts/jNQXAC9IVRw",
        "https://www.youtube.com/live/jNQXAC9IVRw",
        "https://www.youtube.com/embed/jNQXAC9IVRw",
        " jNQXAC9IVRw ",
    ],
)
def test_video_id_from_every_kind_of_link(url):
    assert transcripts.video_id(url) == "jNQXAC9IVRw"


def test_a_link_without_a_video_is_refused():
    with pytest.raises(transcripts.TranscriptError, match="isn't the URL of a YouTube video"):
        transcripts.video_id("https://www.youtube.com/playlist?list=PL123")


def test_without_languages_it_is_in_the_language_spoken_preferring_a_written_one(listing):
    listing(
        FakeTranscript("English", "en", is_generated=False),
        FakeTranscript("Spanish", "es", is_generated=False),
        FakeTranscript("Spanish (auto-generated)", "es", is_generated=True),
    )
    found = transcripts.fetch("jNQXAC9IVRw")
    assert (found.language, found.generated) == ("Spanish", False)
    assert found.text == "Hello there again"


def test_languages_go_in_order_and_missing_ones_list_what_there_is(listing):
    listing(FakeTranscript("English", "en", False), FakeTranscript("German (auto-generated)", "de", True))
    assert transcripts.fetch("jNQXAC9IVRw", ["fr", "en"]).language == "English"
    with pytest.raises(transcripts.TranscriptError) as e:
        transcripts.fetch("jNQXAC9IVRw", ["fr"])
    assert str(e.value) == "It has no transcript in fr; it has: English (en), German (auto-generated) (de)."


def test_timestamps_put_each_caption_on_its_line(listing):
    captions = [Caption("Hi", 1.2), Caption("later", 65.0), Caption("much later", 3725.0)]
    listing(FakeTranscript("English", "en", True, captions))
    assert transcripts.fetch("jNQXAC9IVRw", timestamps=True).text == "[0:01] Hi\n[1:05] later\n[1:02:05] much later"


def test_the_librarys_errors_keep_only_the_cause(listing):
    listing()
    with pytest.raises(transcripts.TranscriptError) as e:
        transcripts.fetch("jNQXAC9IVRw")
    assert "Subtitles are disabled for this video" in str(e.value)
    assert "create an issue" not in str(e.value)


def test_save_names_the_file_after_the_title(listing, monkeypatch, tmp_path):
    listing(FakeTranscript("English", "en", True))
    monkeypatch.setenv("YOUTUBE_DIR", str(tmp_path))
    monkeypatch.setattr(transcripts, "title", lambda vid: 'Me at the zoo: "elephants"?')
    found, path = transcripts.save("https://youtu.be/jNQXAC9IVRw")
    assert path == tmp_path / "Me at the zoo elephants [jNQXAC9IVRw].txt"
    assert path.read_text() == found.text + "\n"
    monkeypatch.setattr(transcripts, "title", lambda vid: "")
    assert transcripts.save("jNQXAC9IVRw")[1].name == "jNQXAC9IVRw [jNQXAC9IVRw].txt"
