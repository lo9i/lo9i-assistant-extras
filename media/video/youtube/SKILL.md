---
name: youtube
description: Downloading YouTube videos, their audio as MP3, and their transcripts (the youtube MCP server). Use it when the user shares a YouTube link and wants the video, the audio or song, the transcript, or to know what the video says.
---

The `youtube` MCP server's tools are `mcp__youtube__download_video`, `mcp__youtube__download_audio`, `mcp__youtube__get_transcript` and `mcp__youtube__download_transcript`. They take a video's URL (youtube.com, youtu.be, Shorts) or its ID.

- **The video**: `download_video`. It's MP4 at up to 1080p; pass `max_height` only when the user asks for another quality (720, 2160 for 4K).
- **The audio, a song, a podcast episode**: `download_audio`, an MP3 with the title, channel and cover.
- **What the video says** (a summary, a question about it, a quote): `get_transcript`, and answer from it. Don't download anything.
- **The transcript as a file**: `download_transcript`. Add `timestamps` if they want to see when each part is said.

Without `languages`, the transcript is in the language spoken in the video. When the user wants another language, pass its code (`["es"]`); if the video has no transcript in it, the error lists the ones it has, so offer those or translate it yourself. A transcript YouTube generated (`generated: true`) has no punctuation and mishears names: say so when quoting it.

A download takes from seconds to a few minutes. When it's done, tell the user the file's name and folder: you can't send them the file. A playlist link without a video in it is refused; ask which video they mean. Download one video per request unless the user asks for several.
