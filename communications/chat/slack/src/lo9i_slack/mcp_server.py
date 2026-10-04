"""MCP server with the Slack workspace tools: search, read and post as the user, with the user token.
lo9i starts it (server.yaml) with the tokens set:

    SLACK_USER_TOKEN=xoxp-... lo9i-slack-mcp

Search and read are marked read-only. Posting isn't, so lo9i asks the user before every post (the
plugin isn't trusted) and denies it in scheduled runs.
"""

import os
from datetime import datetime, tzinfo
from zoneinfo import ZoneInfo

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from slack_sdk.web.async_client import AsyncWebClient

from lo9i_slack.connection import SlackError
from lo9i_slack.workspace import Workspace

mcp = MCPServer("slack")

READ_ONLY = ToolAnnotations(read_only_hint=True)
_NOT_SET_UP = "Slack isn't set up: the user enters the tokens in the Slack plugin's settings in the app."


@mcp.tool(annotations=READ_ONLY)
async def slack_search(query: str, count: int = 20) -> str:
    """Search messages in the user's Slack workspace, newest first. Slack search syntax works, for
    example `budget in:#finance from:@ana after:2026-09-01`."""
    workspace = _workspace()
    if workspace is None:
        return _NOT_SET_UP
    try:
        lines = await workspace.search(query, count)
    except SlackError as e:
        return str(e)
    return "\n\n".join(lines) or "No messages found."


@mcp.tool(annotations=READ_ONLY)
async def slack_read(channel: str, thread: str | None = None, limit: int = 30) -> str:
    """Read the latest messages of a Slack channel (a name like #general, or an id), oldest first.
    With `thread` (a message's ts), read that thread's replies instead."""
    workspace = _workspace()
    if workspace is None:
        return _NOT_SET_UP
    try:
        lines = await workspace.history(channel, thread, limit)
    except SlackError as e:
        return str(e)
    return "\n\n".join(lines) or "No messages."


@mcp.tool()
async def slack_post(channel: str, text: str, thread: str | None = None) -> str:
    """Post a message in Slack as the user, in a channel (a name like #general, or an id), or as a
    reply in `thread` (a message's ts). The user approves every post."""
    workspace = _workspace()
    if workspace is None:
        return _NOT_SET_UP
    try:
        return f"Posted: {await workspace.post(channel, text, thread)}"
    except SlackError as e:
        return str(e)


def _workspace() -> Workspace | None:
    token = os.environ.get("SLACK_USER_TOKEN", "").strip()
    return Workspace(AsyncWebClient(token), _zone()) if token else None


def _zone() -> tzinfo:
    """Times are shown in TZ when it's set, else in this computer's time zone."""
    if name := os.environ.get("TZ"):
        return ZoneInfo(name)
    return datetime.now().astimezone().tzinfo or ZoneInfo("UTC")


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
