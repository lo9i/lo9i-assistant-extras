import pytest

from expenses import db, service


@pytest.fixture(autouse=True)
def no_country(monkeypatch):
    """A time zone in no country, so no currency is guessed unless a test sets TZ."""
    monkeypatch.setenv("TZ", "UTC")


@pytest.fixture
def conn():
    conn = db.connect(":memory:")
    service.add_asset(conn, "House", "house", id="h")
    yield conn
    conn.close()
