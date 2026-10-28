"""MCP server over stdio: Hacker News stories, search and mentions. lo9i starts it (see server.yaml)."""

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from lo9i_hackernews import hn

INSTRUCTIONS = """\
Reads Hacker News (news.ycombinator.com): the front page and other lists, search, and mentions of a word in
stories and comments. Give each story's title with its link, points and comments. It can't post or vote.
"""

mcp = MCPServer("hackernews", instructions=INSTRUCTIONS)
READ_ONLY = ToolAnnotations(read_only_hint=True)


@mcp.tool(annotations=READ_ONLY)
async def top_stories(kind: str = "top", limit: int = 10) -> list[dict]:
    """Stories from an HN list, in its order: `kind` is top (the front page), new, best, show (Show HN) or
    ask (Ask HN); `limit` up to 30. Each has title, url, discussion link, points, comments, author, time."""
    try:
        return await hn.stories(kind, limit)
    except hn.HNError as e:
        raise ToolError(str(e)) from None


@mcp.tool(annotations=READ_ONLY)
async def search(query: str, days: int | None = None, sort: str = "relevance", limit: int = 10) -> list[dict]:
    """Search HN stories. `days`: only the last N days; `sort`: relevance or date (newest first);
    `limit` up to 50."""
    try:
        return await hn.search(query, days, sort, limit)
    except hn.HNError as e:
        raise ToolError(str(e)) from None


@mcp.tool(annotations=READ_ONLY)
async def mentions(word: str, days: int = 7, limit: int = 20) -> list[dict]:
    """Stories and comments that mention `word` (a project's name) in the last `days`, newest first.
    Comments come with their text and the story they're in."""
    try:
        return await hn.mentions(word, days, limit)
    except hn.HNError as e:
        raise ToolError(str(e)) from None


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
