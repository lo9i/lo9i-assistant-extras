import asyncio
import json
import socketserver
import tempfile
import threading
from pathlib import Path

import pytest
from mcp import Client
from starlette.testclient import TestClient

from lo9i_browser import engines, mcp_server, web
from lo9i_browser.live import Live
from lo9i_browser.session import Browser


@pytest.fixture
def short_dir():
    """A folder for Unix sockets, whose paths can't be as long as pytest's tmp_path."""
    with tempfile.TemporaryDirectory(dir="/tmp") as folder:
        yield Path(folder)


@pytest.fixture
async def headless(tmp_path, short_dir, monkeypatch):
    """The server's browser as on a computer without a screen, shown live on a socket of the test's own."""
    if not engines.chrome_installed():
        pytest.skip("needs Google Chrome installed")
    made = Browser(tmp_path, chrome=True, screen=False)
    made.on_window_closed = mcp_server._window_closed
    monkeypatch.setattr(mcp_server, "browser", made)
    monkeypatch.setattr(mcp_server, "live", Live(short_dir / "live.sock", mcp_server.address, mcp_server._done_live))
    yield made
    await mcp_server.live.stop()
    await made.close()


async def _until(reader: asyncio.StreamReader, kind: str) -> dict:
    async with asyncio.timeout(20):
        while True:
            message = json.loads(await reader.readline())
            if message["type"] == kind:
                return message


async def _eventually(check) -> None:
    async with asyncio.timeout(10):
        while not await check():
            await asyncio.sleep(0.1)


async def _says(page, text: str) -> bool:
    return await page.locator("#said").inner_text() == text


async def test_without_a_screen_the_browser_is_shown_live_and_used_from_the_page(headless, site, short_dir):
    async with Client(mcp_server.mcp) as client:
        shown = await client.call_tool("show_window", {"url": f"{site}/index.html"})
        assert "/pages/browser" in shown.content[0].text
        assert headless.visible
        _, page = await headless.tab()

        reader, writer = await asyncio.open_unix_connection(str(short_dir / "live.sock"))
        frame = await _until(reader, "frame")
        assert frame["width"] == 1280 and frame["data"]

        def send(message: dict) -> None:
            writer.write(json.dumps(message).encode() + b"\n")

        box = await page.get_by_role("button", name="Press me").bounding_box()
        assert box is not None
        x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        send({"type": "mouse", "action": "down", "x": x, "y": y, "button": "left", "clicks": 1})
        send({"type": "mouse", "action": "up", "x": x, "y": y, "button": "left", "clicks": 1})
        await _eventually(lambda: _says(page, "The button was clicked."))

        box = await page.locator("#q").bounding_box()
        assert box is not None
        x, y = box["x"] + 5, box["y"] + 5
        send({"type": "mouse", "action": "down", "x": x, "y": y, "button": "left", "clicks": 1})
        send({"type": "mouse", "action": "up", "x": x, "y": y, "button": "left", "clicks": 1})
        send({"type": "text", "text": "dogs"})
        send({"type": "key", "key": "Enter"})
        await _eventually(lambda: _says(page, "Searched dogs"))

        send({"type": "go", "url": "file:///etc/passwd"})
        assert "Only web pages" in (await _until(reader, "problem"))["text"]

        send({"type": "done"})
        await _until(reader, "closed")
        writer.close()

        async def hidden() -> bool:
            return not headless.visible

        await _eventually(hidden)
        assert not mcp_server.live.listening
        assert not (short_dir / "live.sock").exists()
        # The browser goes on as it was, without a window.
        assert headless.context is not None


def test_the_page_says_where_it_is_served_and_whether_the_browser_shows_here(short_dir):
    app = web.create_app(short_dir / "live.sock", screen=False)
    with TestClient(app) as client:
        page = client.get("/", headers={"x-forwarded-prefix": "/browser"})
        assert '<base href="/browser/">' in page.text
        assert 'data-screen="false"' in page.text
        assert 'src="static/live.js?v=' in page.text
        assert client.get("/", headers={"x-forwarded-prefix": "/../x"}).text.count('<base href="/">') == 1
        with client.websocket_connect("/live") as ws:
            assert ws.receive_json() == {"type": "off"}


def test_the_pages_websocket_goes_through_to_the_live_view(short_dir):
    path = short_dir / "live.sock"
    heard = []

    class Live(socketserver.StreamRequestHandler):
        def handle(self):
            self.wfile.write(b'{"type": "page", "url": "https://example.com/", "title": ""}\n')
            heard.append(json.loads(self.rfile.readline()))

    server = socketserver.UnixStreamServer(str(path), Live)
    threading.Thread(target=server.handle_request, daemon=True).start()
    try:
        with TestClient(web.create_app(path, screen=False)) as client, client.websocket_connect("/live") as ws:
            assert ws.receive_json()["url"] == "https://example.com/"
            ws.send_text(json.dumps({"type": "back"}))
            # The live view closes once it heard it, which closes the WebSocket.
            ws.receive()
    finally:
        server.server_close()
    assert heard == [{"type": "back"}]
