"""MCP server over stdio: download a YouTube video, its audio as MP3, or its transcript. lo9i starts it
(see server.yaml)."""

import asyncio
from dataclasses import asdict

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from lo9i_youtube import downloads, transcripts

INSTRUCTIONS = """\
Downloads YouTube videos (MP4) and their audio (MP3) into the user's download folder, and gets
their transcripts. Downloads take from seconds to minutes, depending on the video's length. Tell the
user where the file is: you can't send it to them. To know or summarize what a video says, use
get_transcript; download_transcript is for when the user wants it as a file.
"""

mcp = MCPServer("youtube", instructions=INSTRUCTIONS)

READ_ONLY = ToolAnnotations(read_only_hint=True)


@mcp.tool()
async def download_video(url: str, max_height: int = 1080) -> dict:
    """Download a YouTube video as MP4 into the user's download folder, at the best resolution up to
    `max_height` (720, 1080, 2160…). Returns its title, path and size in MB."""
    try:
        done = await asyncio.to_thread(downloads.video, url, max_height)
    except downloads.DownloadError as e:
        raise ToolError(str(e)) from None
    return asdict(done)


@mcp.tool()
async def download_audio(url: str) -> dict:
    """Download a YouTube video's audio as MP3 into the user's download folder, tagged with its title and
    channel and with its thumbnail as the cover. Returns its title, path and size in MB."""
    try:
        done = await asyncio.to_thread(downloads.audio, url)
    except downloads.DownloadError as e:
        raise ToolError(str(e)) from None
    return asdict(done)


@mcp.tool(annotations=READ_ONLY)
async def get_transcript(url: str, languages: list[str] | None = None, timestamps: bool = False) -> dict:
    """A YouTube video's transcript: its language, whether YouTube generated it (rather than the uploader
    writing it) and its text. `languages` are codes in order of preference (["es", "en"]); without them,
    the language spoken in the video. With `timestamps`, one line per caption starting with [m:ss]."""
    try:
        found = await asyncio.to_thread(transcripts.fetch, url, languages, timestamps)
    except transcripts.TranscriptError as e:
        raise ToolError(str(e)) from None
    return asdict(found)


@mcp.tool()
async def download_transcript(url: str, languages: list[str] | None = None, timestamps: bool = False) -> dict:
    """Save a YouTube video's transcript as a text file in the user's download folder; the arguments are
    get_transcript's. Returns its language, whether YouTube generated it, and the file's path."""
    try:
        found, path = await asyncio.to_thread(transcripts.save, url, languages, timestamps)
    except transcripts.TranscriptError as e:
        raise ToolError(str(e)) from None
    return {"language": found.language, "generated": found.generated, "path": str(path)}


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
