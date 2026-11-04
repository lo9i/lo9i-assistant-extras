import http.server
import threading
from pathlib import Path

import pytest

from lo9i_browser import engines, mcp_server
from lo9i_browser.session import Browser

PAGES = Path(__file__).parent / "pages"


@pytest.fixture(scope="session")
def site():
    """The test pages, served on localhost."""

    class Quiet(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(PAGES), **kwargs)

        def log_message(self, format, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Quiet)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


@pytest.fixture
async def browser(tmp_path, monkeypatch):
    """The server's browser on a profile of the test's own, with Google Chrome (no download in tests)."""
    if not engines.chrome_installed():
        pytest.skip("needs Google Chrome installed")
    made = Browser(tmp_path, chrome=True)
    monkeypatch.setattr(mcp_server, "browser", made)
    yield made
    await made.close()
