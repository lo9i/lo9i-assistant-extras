"""Turns files shared in a Slack message (images, documents, voice and audio clips) into attachments."""

from typing import Any

import httpx

from lo9i_slack.daemon import Attachment

_TIMEOUT = 60


class UnsupportedMediaError(Exception):
    pass


async def attachments_from(files: list[dict[str, Any]], bot_token: str) -> list[Attachment]:
    """Downloads with the bot token: Slack file URLs need it (`files:read`)."""
    if any(f.get("mimetype", "").startswith("video/") for f in files):
        raise UnsupportedMediaError("Video isn't supported.")
    headers = {"Authorization": f"Bearer {bot_token}"}
    async with httpx.AsyncClient(timeout=_TIMEOUT, headers=headers, follow_redirects=True) as client:
        return [await _download(client, f) for f in files]


async def _download(client: httpx.AsyncClient, file: dict[str, Any]) -> Attachment:
    response = await client.get(file["url_private_download"])
    response.raise_for_status()
    name = file.get("name") or f"file-{file['id']}"
    return Attachment(name=name, media_type=file.get("mimetype") or "application/octet-stream", data=response.content)
