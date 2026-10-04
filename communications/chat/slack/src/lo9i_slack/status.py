"""What the apps show for Slack: the workspace and its owner, or why the channel isn't running, and
the app manifest to create the Slack app from, named after the assistant."""

from lo9i_chat import status
from lo9i_chat.status import SetupItem
from lo9i_slack.connection import Owner, manifest


def items(connection: Owner | str, assistant_name: str) -> list[SetupItem]:
    """`connection` is the workspace's owner, or the reason Slack didn't connect."""
    line = connection if isinstance(connection, str) else f"Workspace: {connection.team} · owner: {connection.name}"
    return [status.text(line), status.copy("App manifest", manifest(assistant_name))]
