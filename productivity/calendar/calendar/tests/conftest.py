"""The user's time zone for every test, and a CalDAV server to talk to: Radicale, in a thread."""

import logging
import sys
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from wsgiref.simple_server import WSGIRequestHandler, make_server

import pytest
from caldav.collection import Calendar
from caldav.davclient import DAVClient
from radicale import config
from radicale.app import Application

from lo9i_calendar import service

ZONE = "America/Argentina/Buenos_Aires"


@pytest.fixture(autouse=True)
def user_zone(monkeypatch):
    monkeypatch.setenv("TZ", ZONE)
    for key in ("CALDAV_URL", "CALDAV_USERNAME", "CALDAV_PASSWORD"):
        monkeypatch.delenv(key, raising=False)
    sources = service.sources  # some tests replace it
    sources.cache_clear()
    yield
    sources.cache_clear()


class _Quiet(WSGIRequestHandler):
    def log_message(self, format: str, *args) -> None:
        pass


@dataclass
class Dav:
    url: str
    work: Calendar
    home: Calendar


@pytest.fixture
def dav(tmp_path, monkeypatch) -> Iterator[Dav]:
    """A CalDAV account with a Work and a Home calendar, set up as the plugin's fields, off a Mac."""
    (tmp_path / "users").write_text("ana:secret\n")
    settings = config.load()
    settings.update(
        {
            "storage": {"filesystem_folder": str(tmp_path / "collections")},
            "auth": {
                "type": "htpasswd",
                "htpasswd_filename": str(tmp_path / "users"),
                "htpasswd_encryption": "plain",
                # Radicale waits after a failed login; caldav's first request is one.
                "delay": "0",
            },
            "rights": {"type": "owner_only"},
        },
        "tests",
        privileged=True,
    )
    logging.getLogger("radicale").setLevel(logging.CRITICAL)
    server = make_server("127.0.0.1", 0, Application(settings), handler_class=_Quiet)
    threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}/"
    principal = DAVClient(url=url, username="ana", password="secret").principal()
    work, home = principal.make_calendar(name="Work"), principal.make_calendar(name="Home")
    assert isinstance(work, Calendar) and isinstance(home, Calendar)
    account = Dav(url, work, home)
    monkeypatch.setenv("CALDAV_URL", url)
    monkeypatch.setenv("CALDAV_USERNAME", "ana")
    monkeypatch.setenv("CALDAV_PASSWORD", "secret")
    monkeypatch.setattr(sys, "platform", "linux")
    yield account
    server.shutdown()
    server.server_close()
