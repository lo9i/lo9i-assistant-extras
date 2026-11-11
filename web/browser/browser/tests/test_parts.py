import json

from lo9i_browser import engines, lo9i, places, reading


def test_text_is_tidied():
    assert reading.tidy("  a \t b\xa0c \n\n\n\n d \n") == "a b c\n\nd"


def test_text_comes_in_parts():
    assert reading.part("abcdef", 0, 4) == {"text": "abcd", "chars": 6, "offset": 0, "next_offset": 4}
    assert reading.part("abcdef", 4, 4) == {"text": "ef", "chars": 6, "offset": 4}
    assert reading.part("abcdef", -3, 0)["text"] == "a"
    assert len(reading.part("x" * 60_000, 0, 99_999)["text"]) == reading.MAX_CHARS


def test_the_profile_keeps_the_browser_it_was_made_with(tmp_path):
    assert engines.chosen(tmp_path, chrome=True) == engines.CHROME
    assert json.loads((tmp_path / "engine.json").read_text()) == {"engine": "chrome"}
    # Chrome uninstalled: Chromium, and it stays so once Chrome is back.
    assert engines.chosen(tmp_path, chrome=False) == engines.CHROMIUM
    assert engines.chosen(tmp_path, chrome=True) == engines.CHROMIUM


def test_without_chrome_the_profile_is_chromiums(tmp_path):
    (tmp_path / "engine.json").write_text("not json")
    assert engines.chosen(tmp_path, chrome=False) == engines.CHROMIUM


def test_without_chrome_the_systems_chromium_comes_before_a_download(tmp_path):
    assert engines.chosen(tmp_path, chrome=False, system=True) == engines.SYSTEM
    # Kept while it's there, Chrome installed later too; Playwright's once it's gone.
    assert engines.chosen(tmp_path, chrome=True, system=True) == engines.SYSTEM
    assert engines.chosen(tmp_path, chrome=False, system=False) == engines.CHROMIUM
    assert engines.chosen(tmp_path / "chrome", chrome=True, system=True) == engines.CHROME


def test_the_systems_chromium_launches_by_its_path(monkeypatch, tmp_path):
    chromium = tmp_path / "chromium"
    chromium.touch()
    monkeypatch.setitem(engines.SYSTEM_CHROMIUM_PATHS, "linux", chromium)
    monkeypatch.setattr(engines.sys, "platform", "linux")
    assert engines.launching(engines.SYSTEM) == {"executable_path": str(chromium)}
    assert engines.launching(engines.CHROME) == {"channel": "chrome"}
    assert engines.system_chromium("darwin") is None


def test_only_linux_without_a_display_has_no_screen(monkeypatch):
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    assert not places.has_screen("linux")
    assert places.has_screen("darwin")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    assert places.has_screen("linux")


class _Session:
    def __init__(self):
        self.sent = []

    async def send_notification(self, notification):
        self.sent.append((notification.method, notification.params))


ORIGIN = {"user_id": "owner", "thread_id": "t1", "channel": "web"}


async def test_the_window_holds_the_server_until_it_closes():
    window, session = lo9i.Window(), _Session()
    await window.shown(session, ORIGIN)
    await window.shown(session, ORIGIN)
    await window.closed("Closed.")
    await window.closed("Closed again.")
    assert session.sent == [
        (lo9i.STARTED, {"work": "window"}),
        (lo9i.FINISHED, {"work": "window", "origin": ORIGIN, "ok": True, "text": "Closed."}),
    ]


async def test_a_window_shown_outside_lo9i_tells_nothing():
    window, session = lo9i.Window(), _Session()
    await window.shown(session, None)
    await window.closed("Closed.")
    assert session.sent == []


def test_the_origin_comes_from_the_calls_meta():
    assert lo9i.origin({"lo9i/origin": ORIGIN}) == ORIGIN
    assert lo9i.origin(None) is None
    assert lo9i.origin({"lo9i/origin": "x"}) is None
