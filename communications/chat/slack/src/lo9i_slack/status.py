"""What the apps show for Slack, in the language the user chose: the workspace and its owner, or why the channel
isn't running, and the app manifest to create the Slack app from, named after the assistant. Each item is `kind`
(text, code, copy or action), `label` and `value`."""

from lo9i_slack.connection import Owner, SlackError, manifest
from lo9i_slack.i18n import Language


def items(
    t: Language, connection: Owner | SlackError, assistant_name: str, commands: list[dict[str, str]]
) -> list[dict[str, str]]:
    """`connection` is the workspace's owner, or why Slack didn't connect. The manifest lists lo9i's
    commands, so the app offers them as slash commands once it's updated with it."""
    if isinstance(connection, SlackError):
        line = connection.shown(t)
    else:
        line = t("Workspace: {team} · owner: {name}", team=connection.team, name=connection.name)
    return [
        {"kind": "text", "label": "", "value": line},
        {"kind": "copy", "label": t("App manifest"), "value": manifest(assistant_name, commands, t)},
    ]
