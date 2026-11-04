import json
from pathlib import Path

import pytest
import yaml
from mcp import Client
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import TextContent

from lo9i_browser import mcp_server

SERVER_YAML = yaml.safe_load((Path(__file__).parents[1] / "server.yaml").read_text())
READS = {"open_page", "read_page", "screenshot", "scroll", "list_tabs"}
ACTS = {"click", "type_text", "press_key"}


def _text(result) -> str:
    first = result.content[0]
    assert isinstance(first, TextContent)
    return first.text


def _json(result) -> dict:
    assert not result.is_error, result.content
    return json.loads(_text(result))


async def _tools():
    async with Client(mcp_server.mcp) as client:
        return {t.name: t for t in (await client.list_tools()).tools}


async def test_reading_tools_are_read_only_and_acting_ones_ask_every_time():
    tools = await _tools()
    assert set(tools) == READS | ACTS | {"close_tab", "show_window", "hide_window"}
    read_only = {name for name, t in tools.items() if t.annotations and t.annotations.read_only_hint}
    assert read_only == READS
    assert SERVER_YAML["trusted"] is True
    assert set(SERVER_YAML["approve"]) == ACTS


def test_server_yaml_gives_the_data_folder_and_the_format_for_approve():
    assert SERVER_YAML["env"]["BROWSER_DATA"] == "{data}"
    assert SERVER_YAML["format"] >= 7
    assert SERVER_YAML["command"] == "{bin}/browser-mcp"


@pytest.mark.parametrize(
    ("given", "opened"),
    [
        ("youtube.com", "https://youtube.com"),
        ("  https://www.youtube.com/feed/subscriptions ", "https://www.youtube.com/feed/subscriptions"),
        ("http://example.com/a?b=1", "http://example.com/a?b=1"),
    ],
)
def test_an_address_opens_on_the_web(given, opened):
    assert mcp_server.address(given) == opened


@pytest.mark.parametrize(
    "given", ["file:///etc/passwd", "javascript:alert(1)", "chrome://settings", "about:blank", "data:text/html,hi"]
)
def test_only_web_pages_open(given):
    with pytest.raises(ToolError, match="Only web pages"):
        mcp_server.address(given)


def test_the_window_needs_the_users_screen(monkeypatch):
    monkeypatch.setenv("LO9I_LOGIN_SESSION", "/tmp/session.sock")
    assert "outside the user's login session" in mcp_server._no_screen()
    monkeypatch.delenv("LO9I_LOGIN_SESSION")
    monkeypatch.setattr(mcp_server.sys, "platform", "linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    assert "no screen" in mcp_server._no_screen()
    monkeypatch.setenv("DISPLAY", ":0")
    assert mcp_server._no_screen() == ""


async def test_the_page_is_read_from_the_browser(browser, site):
    async with Client(mcp_server.mcp) as client:
        opened = await client.call_tool("open_page", {"url": f"{site}/index.html"})
        assert _json(opened)["title"] == "Test home"
        assert "Welcome to the test" in _json(opened)["text"]
        assert _json(opened)["tab"] == 1

        part = _json(await client.call_tool("read_page", {"offset": 8, "max_chars": 10}))
        assert part["text"] == "to the tes"
        assert part["next_offset"] == 18

        links = _json(await client.call_tool("read_page", {"links": True}))["links"]
        assert {"text": "Other page", "url": f"{site}/other.html"} in links
        assert all(link["url"].startswith("http") for link in links)
        assert "Hidden link" not in {link["text"] for link in links}


async def test_clicking_and_typing_act_on_the_page(browser, site):
    async with Client(mcp_server.mcp) as client:
        await client.call_tool("open_page", {"url": f"{site}/index.html"})
        await client.call_tool("click", {"text": "Press me"})
        assert "The button was clicked." in _json(await client.call_tool("read_page", {}))["text"]

        await client.call_tool("type_text", {"text": "cats", "field": "Search", "submit": True})
        assert "Searched cats" in _json(await client.call_tool("read_page", {}))["text"]

        missing = await client.call_tool("click", {"text": "No such button"})
        assert missing.is_error
        assert "No element shown on the page has the words 'No such button'" in _text(missing)


async def test_a_link_to_a_new_tab_opens_it_and_tabs_are_listed(browser, site):
    async with Client(mcp_server.mcp) as client:
        await client.call_tool("open_page", {"url": f"{site}/index.html"})
        clicked = _json(await client.call_tool("click", {"text": "Other in a new tab"}))
        assert clicked["opened_tab"]["title"] == "Other"
        assert _json(await client.call_tool("read_page", {}))["text"] == "The other page."

        tabs = _json(await client.call_tool("list_tabs", {}))
        assert [(t["tab"], t["title"], t["current"]) for t in tabs["tabs"]] == [
            (1, "Test home", False),
            (2, "Other", True),
        ]
        assert tabs["window_shown"] is False

        await client.call_tool("close_tab", {"tab": 2})
        assert _json(await client.call_tool("read_page", {"tab": 1}))["title"] == "Test home"
        gone = await client.call_tool("read_page", {"tab": 2})
        assert gone.is_error and "There's no tab 2" in _text(gone)


async def test_scrolling_reaches_the_bottom_and_a_screenshot_is_an_image(browser, site):
    async with Client(mcp_server.mcp) as client:
        await client.call_tool("open_page", {"url": f"{site}/index.html"})
        down = _json(await client.call_tool("scroll", {"screens": 10}))
        assert down["at_bottom"] and not down["at_top"]
        up = _json(await client.call_tool("scroll", {"direction": "up", "screens": 10}))
        assert up["at_top"]

        shot = await client.call_tool("screenshot", {})
        assert "Test home" in _text(shot)
        assert shot.content[1].type == "image" and shot.content[1].mime_type == "image/jpeg"


async def test_sign_ins_stay_in_the_profile(browser, site):
    """A cookie a site sets is there when the browser starts again on the same profile."""
    async with Client(mcp_server.mcp) as client:
        await client.call_tool("open_page", {"url": f"{site}/index.html"})
        _, page = await browser.tab()
        await page.evaluate("document.cookie = 'signed_in=yes; max-age=3600'")
        await browser.close()
        await client.call_tool("open_page", {"url": f"{site}/index.html"})
        _, page = await browser.tab()
        assert "signed_in=yes" in await page.evaluate("document.cookie")


@pytest.mark.live
async def test_youtube_reads_without_a_window(browser):
    async with Client(mcp_server.mcp) as client:
        page = _json(await client.call_tool("open_page", {"url": "https://www.youtube.com"}))
    assert page["title"] == "YouTube"
    assert "Home" in page["text"]
    print(json.dumps(page, ensure_ascii=False)[:600])
