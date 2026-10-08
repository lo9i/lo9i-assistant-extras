"""What the apps show for Slack: the workspace and its owner, or why the channel isn't running, and
the app manifest to create the Slack app from, named after the assistant. Each item is `kind` (text,
code, copy or action), `label` and `value`."""

from lo9i_slack.connection import Owner, manifest


def items(connection: Owner | str, assistant_name: str) -> list[dict[str, str]]:
    """`connection` is the workspace's owner, or the reason Slack didn't connect."""
    line = connection if isinstance(connection, str) else f"Workspace: {connection.team} · owner: {connection.name}"
    return [
        {"kind": "text", "label": "", "value": line},
        {"kind": "copy", "label": "App manifest", "value": manifest(assistant_name)},
    ]
