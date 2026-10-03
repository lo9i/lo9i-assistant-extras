import pytest

from expenses import db, service


@pytest.fixture
def conn():
    conn = db.connect(":memory:")
    service.add_asset(conn, "House", "house", id="h")
    yield conn
    conn.close()
