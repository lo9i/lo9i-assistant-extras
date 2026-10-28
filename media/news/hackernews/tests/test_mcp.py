import json

from mcp import Client

from lo9i_hackernews import hn, mcp_server


async def _call(tool, /, **args):
    async with Client(mcp_server.mcp) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        result = await client.call_tool(tool, args)
    return tools, result


async def test_every_tool_is_read_only(monkeypatch):
    async def stories(kind, limit):
        return [{"title": "Hi"}]

    monkeypatch.setattr(hn, "stories", stories)
    tools, result = await _call("top_stories", kind="show", limit=1)
    assert all(t.annotations and t.annotations.read_only_hint for t in tools.values())
    assert set(tools) == {"top_stories", "search", "mentions"}
    assert json.loads(result.content[0].text) == {"title": "Hi"} or "Hi" in result.content[0].text


def test_a_comment_hit_links_its_story():
    hit = {
        "objectID": "2",
        "story_id": 1,
        "_tags": ["comment"],
        "story_title": "Show HN: lo9i",
        "author": "a",
        "comment_text": "nice",
        "created_at": "2026-10-29T14:00:00Z",
    }
    assert hn._hit(hit) == {
        "kind": "comment",
        "title": "Show HN: lo9i",
        "discussion": "https://news.ycombinator.com/item?id=1",
        "link": "https://news.ycombinator.com/item?id=2",
        "by": "a",
        "time": "2026-10-29T14:00:00Z",
        "text": "nice",
    }
