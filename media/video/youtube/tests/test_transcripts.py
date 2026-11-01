from dataclasses import dataclass, field

import pytest
from youtube_transcript_api import NoTranscriptFound, TranscriptsDisabled

from lo9i_youtube import transcripts


@dataclass
class Caption:
    text: str
    start: float
    duration: float = 2.0


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


@pytest.fixture(autouse=True)
def about(monkeypatch):
    monkeypatch.setattr(transcripts, "about", lambda vid: ("Me at the zoo", "jawed"))


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
    assert (found.title, found.channel, found.duration) == ("Me at the zoo", "jawed", "1:07")
    assert (found.text, found.part, found.parts) == ("[0:01] Hello there again", 1, 1)


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


def test_sections_start_every_five_minutes_with_their_time(listing):
    captions = [Caption("one", 0.5), Caption("two", 200.0), Caption("three", 301.0), Caption("four", 3700.0)]
    listing(FakeTranscript("English", "en", True, captions))
    assert transcripts.fetch("jNQXAC9IVRw").text == "[0:00] one two\n\n[5:01] three\n\n[1:01:40] four"


def test_repeats_and_sound_tags_are_left_out(listing):
    captions = [
        Caption("[Music]", 0.0),
        Caption("so today we talk", 1.0),
        Caption("so today we talk", 2.0),
        Caption("today we talk about whales", 3.0),
        Caption("whales [Applause] are big", 4.0),
        Caption("[Music]", 5.0),
    ]
    listing(FakeTranscript("English", "en", True, captions))
    # "whales" repeats only one word: that's speech ("that that"), not a repeated caption.
    assert transcripts.fetch("jNQXAC9IVRw").text == "[0:01] so today we talk about whales whales are big"


def test_a_long_transcript_comes_in_parts_of_whole_sections(listing, monkeypatch):
    monkeypatch.setattr(transcripts, "PART_BYTES", 45)
    captions = [Caption(letter * 12, i * 300.0) for i, letter in enumerate("abcdef")]
    listing(FakeTranscript("English", "en", True, captions))
    first = transcripts.fetch("jNQXAC9IVRw")
    assert (first.text, first.part, first.parts) == (f"[0:00] {'a' * 12}\n\n[5:00] {'b' * 12}", 1, 3)
    assert transcripts.fetch("jNQXAC9IVRw", part=3).text == f"[20:00] {'e' * 12}\n\n[25:00] {'f' * 12}"
    with pytest.raises(transcripts.TranscriptError, match="no part 4: it has 3"):
        transcripts.fetch("jNQXAC9IVRw", part=4)


def test_the_librarys_errors_keep_only_the_cause(listing):
    listing()
    with pytest.raises(transcripts.TranscriptError) as e:
        transcripts.fetch("jNQXAC9IVRw")
    assert "Subtitles are disabled for this video" in str(e.value)
    assert "create an issue" not in str(e.value)


def test_save_names_the_file_after_the_title(listing, monkeypatch, tmp_path):
    listing(FakeTranscript("English", "en", True, [Caption("Hi", 1.0), Caption("there", 400.0)]))
    monkeypatch.setenv("YOUTUBE_DIR", str(tmp_path))
    monkeypatch.setattr(transcripts, "about", lambda vid: ('Me at the zoo: "elephants"?', "jawed"))
    found, path = transcripts.save("https://youtu.be/jNQXAC9IVRw")
    assert path == tmp_path / "Me at the zoo elephants [jNQXAC9IVRw].txt"
    assert path.read_text() == "Hi\n\nthere\n"
    assert found.language == "English"
    assert transcripts.save("jNQXAC9IVRw", timestamps=True)[1].read_text() == "[0:01] Hi\n[6:40] there\n"
    monkeypatch.setattr(transcripts, "about", lambda vid: ("", ""))
    assert transcripts.save("jNQXAC9IVRw")[1].name == "jNQXAC9IVRw [jNQXAC9IVRw].txt"
